"""
The EmuHawk client of the automated routing search, against a stand-in for ``bridge.lua``.

The stand-in is a small TCP server on 127.0.0.1, on a port that the operating system picks. It
records each request and answers it as the bridge does, so no EmuHawk is started or reached.

    python -m pytest worlds/cvaos/test/test_autoroute_bizhawk.py -v
"""
from __future__ import annotations

import json
import socket
import threading
import unittest

from ..tools.autoroute import bizhawk
from ..tools.autoroute.bizhawk import BizHawk, BizHawkError


class FakeBridge:
    """A stand-in for bridge.lua that serves one connection.

    ``answer(request)`` returns the lines to send back, or None to close the connection. By
    default each request gets a reply with its id, a read gets the bytes of ``memory``, a write
    goes into ``memory``, and a ping gets frame 12.
    """

    def __init__(self, answer=None) -> None:
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        if self.port in (bizhawk.PORT, 43090):
            raise AssertionError("the OS chose a port that an EmuHawk may be using")
        self.requests: list[dict] = []
        self.memory: dict[int, int] = {}
        self.answer = answer or self.reply
        threading.Thread(target=self.serve, daemon=True).start()

    def reply(self, request: dict) -> list[dict]:
        out = {"id": request["id"]}
        if request["cmd"] == "read":
            out["data"] = [[self.memory.get(address + offset, 0) for offset in range(length)]
                           for address, length in request["reads"]]
        elif request["cmd"] == "write":
            for address, data in request["writes"]:
                for offset, value in enumerate(data):
                    self.memory[address + offset] = value
        elif request["cmd"] == "ping":
            out["frame"] = 12
        return [out]

    def serve(self) -> None:
        with self.server:
            connection, _ = self.server.accept()
            with connection, connection.makefile("rb") as reader:
                for line in reader:
                    request = json.loads(line)
                    self.requests.append(request)
                    replies = self.answer(request)
                    if replies is None:
                        break
                    for out in replies:
                        connection.sendall((json.dumps(out) + "\n").encode())

    def commands(self) -> list[tuple[str, dict]]:
        """Return each request as its command and its arguments, without the id."""
        return [(request["cmd"], {key: value for key, value in request.items()
                                  if key not in ("id", "cmd")}) for request in self.requests]


def connect(bridge: FakeBridge, case: unittest.TestCase, **options) -> BizHawk:
    hawk = BizHawk(port=bridge.port, wait=5.0, timeout=5.0, **options)
    case.addCleanup(hawk.close)
    return hawk


class TestBizHawk(unittest.TestCase):
    def test_a_connection_holds_the_game_without_the_speed_limit_until_it_closes(self):
        bridge = FakeBridge()
        hawk = connect(bridge, self)
        self.assertEqual(bridge.commands(),
                         [("hold", {"on": True}), ("speed", {"throttle": False})])
        hawk.close()
        self.assertEqual(bridge.commands()[2:], [("speed", {"throttle": True}),
                                                 ("hold", {"on": False})])

    def test_a_connection_made_with_fast_false_leaves_the_speed_alone(self):
        bridge = FakeBridge()
        with BizHawk(port=bridge.port, wait=5.0, timeout=5.0, fast=False):
            pass
        self.assertEqual(bridge.commands(), [("hold", {"on": True}), ("hold", {"on": False})])

    def test_reads_and_writes_send_every_range_and_pair_in_one_request(self):
        bridge = FakeBridge()
        hawk = connect(bridge, self)
        hawk.write_many([(0x02000088, b"\x01\x02"), (0x02000090, b"\x03")])
        self.assertEqual(hawk.read_many([(0x02000088, 2), (0x02000090, 1)]),
                         [b"\x01\x02", b"\x03"])
        self.assertEqual(hawk.read(0x02000089, 1), b"\x02")
        self.assertEqual(bridge.commands()[2:5],
                         [("write", {"writes": [[0x02000088, [1, 2]], [0x02000090, [3]]]}),
                          ("read", {"reads": [[0x02000088, 2], [0x02000090, 1]]}),
                          ("read", {"reads": [[0x02000089, 1]]})])

    def test_a_step_sends_its_buttons_in_the_order_of_buttons(self):
        bridge = FakeBridge()
        hawk = connect(bridge, self)
        hawk.step(["Left", "A"], 3)
        self.assertEqual(bridge.commands()[-1], ("step", {"buttons": ["A", "Left"], "frames": 3}))

    def test_a_step_with_an_unknown_button_raises_and_sends_nothing(self):
        bridge = FakeBridge()
        hawk = connect(bridge, self)
        with self.assertRaises(ValueError):
            hawk.step(["Turbo"])
        self.assertEqual(len(bridge.requests), 2)

    def test_states_are_named_slots_and_closing_drops_the_ones_left(self):
        bridge = FakeBridge()
        hawk = connect(bridge, self)
        first, second = hawk.save(), hawk.save()
        self.assertNotEqual(first, second)
        hawk.load(first)
        hawk.drop(first)
        hawk.close()
        self.assertEqual(bridge.commands()[2:], [
            ("state_save", {"slot": first}), ("state_save", {"slot": second}),
            ("state_load", {"slot": first}), ("state_drop", {"slot": first}),
            ("state_drop", {"slot": second}), ("speed", {"throttle": True}),
            ("hold", {"on": False})])

    def test_a_reset_reboots_with_a_blank_save_and_forgets_the_states(self):
        bridge = FakeBridge()
        hawk = connect(bridge, self)
        hawk.save()
        hawk.reset()
        hawk.close()
        self.assertEqual([command for command, _ in bridge.commands()[2:]],
                         ["state_save", "reboot", "speed", "hold"])
        self.assertEqual(bridge.commands()[3], ("reboot", {"keep_save": False}))

    def test_an_event_sent_before_a_reply_is_passed_over(self):
        def answer(request):
            event = {"event": "hotkey", "key": "F1"}
            return [event, *bridge.reply(request)]

        bridge = FakeBridge(answer)
        hawk = connect(bridge, self)
        self.assertEqual(hawk.frame(), 12)

    def test_an_error_reply_raises_bizhawk_error_and_the_connection_stays_open(self):
        def answer(request):
            if request["cmd"] == "state_load":
                return [{"id": request["id"], "error": "no state named x"}]
            return bridge.reply(request)

        bridge = FakeBridge(answer)
        hawk = connect(bridge, self)
        with self.assertRaisesRegex(BizHawkError, "no state named x"):
            hawk.load("x")
        self.assertEqual(hawk.frame(), 12)

    def test_a_dropped_connection_raises_bizhawk_error_and_stays_closed(self):
        def answer(request):
            return None if request["cmd"] == "ping" else bridge.reply(request)

        bridge = FakeBridge(answer)
        hawk = connect(bridge, self)
        with self.assertRaises(BizHawkError):
            hawk.frame()
        with self.assertRaisesRegex(BizHawkError, "closed"):
            hawk.read(0x02000088, 1)
        hawk.close()


if __name__ == "__main__":
    unittest.main()
