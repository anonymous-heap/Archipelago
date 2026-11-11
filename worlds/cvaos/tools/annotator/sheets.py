"""
Sign in to Google and call the few Sheets API methods the annotator needs.

The annotator asks only for the ``drive.file`` scope, which reaches the files a person picked in
Google's file picker and nothing else. Its settings live in the annotator's data folder,
``%LOCALAPPDATA%\\cvaos-annotator``:

* ``google.json``: the project number, the picker's API key and the scope;
* ``client_secret.json``: the OAuth client for desktop apps;
* ``token.json``: the signed-in person's token, written after the first sign-in.

Requests go through ``google.auth``'s ``AuthorizedSession``, which refreshes the token when it
expires. Values are read as the sheet shows them, so checkboxes read "TRUE" and "FALSE" as in
the CSV export. They are written with ``RAW`` input, so text stays text and a JSON boolean sets a
checkbox. Dates are the exception, and go through ``USER_ENTERED`` so that the sheet stores a date.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Iterable, Sequence
from urllib.parse import quote

DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "cvaos-annotator")
API = "https://sheets.googleapis.com/v4/spreadsheets"


class NeedsSignIn(Exception):
    """Raised when no usable token is saved, so the person has to sign in first."""


class NoAccess(Exception):
    """Raised when the signed-in person hasn't picked the sheet in the file picker yet."""


def data_path(name: str) -> str:
    return os.path.join(DATA_DIR, name)


def google_config() -> dict:
    """Return ``google.json``: project number, picker API key and scope."""
    with open(data_path("google.json"), encoding="utf-8") as handle:
        return json.load(handle)


def load_credentials():
    """Return the saved credentials, refreshed when needed, or raise :class:`NeedsSignIn`."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = data_path("token.json")
    if not os.path.exists(path):
        raise NeedsSignIn()
    creds = Credentials.from_authorized_user_file(path, [google_config()["scope"]])
    if not creds.valid:
        if not creds.refresh_token:
            raise NeedsSignIn()
        creds.refresh(Request())
        save_credentials(creds)
    return creds


def save_credentials(creds) -> None:
    with open(data_path("token.json"), "w", encoding="utf-8") as handle:
        handle.write(creds.to_json())


def sign_in():
    """Open the browser for Google sign-in, wait for it, then save and return the credentials.

    This blocks until the person finishes, so the server runs it in a worker thread.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(data_path("client_secret.json"),
                                                     [google_config()["scope"]])
    creds = flow.run_local_server(port=0, open_browser=True,
                                  success_message="Signed in. You can close this tab.")
    save_credentials(creds)
    return creds


@dataclass(frozen=True)
class Tab:
    """One tab of a spreadsheet: its title, its id (the "gid" in links), and its size."""
    title: str
    sheet_id: int
    rows: int
    columns: int


def column_letter(index: int) -> str:
    """Return the column name for a 0-based index: 0 is "A", 26 is "AA"."""
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


class SheetClient:
    """The Sheets API calls the annotator makes, on one spreadsheet."""

    def __init__(self, spreadsheet_id: str, creds=None):
        from google.auth.transport.requests import AuthorizedSession

        self.spreadsheet_id = spreadsheet_id
        self.session = AuthorizedSession(creds or load_credentials())

    def _call(self, method: str, path: str = "", **kwargs) -> dict:
        response = self.session.request(method, f"{API}/{self.spreadsheet_id}{path}", timeout=60,
                                        **kwargs)
        if response.status_code in (403, 404):
            raise NoAccess(response.text[:300])
        response.raise_for_status()
        return response.json() if response.content else {}

    def tab(self, title: str) -> Tab:
        meta = self._call("GET", params={"fields": "sheets.properties"})
        for sheet in meta.get("sheets", []):
            props = sheet["properties"]
            if props["title"] == title:
                grid = props.get("gridProperties", {})
                return Tab(title, props["sheetId"], grid.get("rowCount", 0),
                           grid.get("columnCount", 0))
        raise KeyError(f"no tab named {title!r}")

    def values(self, a1_range: str) -> list[list[str]]:
        """Return the cells of ``a1_range`` as the sheet shows them, row by row."""
        data = self._call("GET", f"/values/{quote(a1_range, safe='')}",
                          params={"valueRenderOption": "FORMATTED_VALUE"})
        return data.get("values", [])

    def write(self, updates: Sequence[tuple[str, list[list[Any]]]],
              user_entered: bool = False) -> None:
        """Write each ``(range, rows)`` pair in one call, with RAW input unless ``user_entered``."""
        if not updates:
            return
        self._call("POST", "/values:batchUpdate", json={
            "valueInputOption": "USER_ENTERED" if user_entered else "RAW",
            "data": [{"range": a1, "values": rows} for a1, rows in updates]})

    def clear(self, ranges: Iterable[str]) -> None:
        """Empty the cells of ``ranges``. Writing "" would leave empty text, which COUNTA counts."""
        ranges = list(ranges)
        if ranges:
            self._call("POST", "/values:batchClear", json={"ranges": ranges})

    def insert_rows(self, tab: Tab, after_row: int, count: int = 1) -> None:
        """Insert ``count`` empty rows below the 1-based row ``after_row``, copying its format."""
        self._call("POST", ":batchUpdate", json={"requests": [{"insertDimension": {
            "range": {"sheetId": tab.sheet_id, "dimension": "ROWS",
                      "startIndex": after_row, "endIndex": after_row + count},
            "inheritFromBefore": True}}]})

    def validation_options(self, a1_cell: str) -> list[str]:
        """Return the dropdown options of one cell, or an empty list when it has none."""
        data = self._call("GET", params={"ranges": a1_cell,
                                         "fields": "sheets.data.rowData.values.dataValidation"})
        try:
            rule = data["sheets"][0]["data"][0]["rowData"][0]["values"][0]["dataValidation"]
        except (KeyError, IndexError):
            return []
        return [value.get("userEnteredValue", "") for value in rule["condition"].get("values", [])]
