import base64
import logging
import os
from typing import ClassVar, Sequence

from BaseClasses import CollectionState, Item, ItemClassification, MultiWorld, Tutorial
from worlds.AutoWorld import WebWorld, World
from worlds.LauncherComponents import Component, Type, components, launch as launch_component
import settings

from .constants import AC_DEFAULT_EXE_PATH, AC_GAME_EXE_MD5, AC_USA_ROM_MD5, USA_ROM_MD5
from .items import (FILLER_ITEM_NAMES, PROGRESSIVE_MOVEMENT, PROGRESSIVE_MOVEMENT_STAGES, CVAOSItem,
                    item_name_to_id, create_item, create_itempool)
from .locations import location_name_to_id
from .options import LOGIC_OPTION_NAMES, CVAOSOptions, Goal, cvaos_option_groups
from .regions import UT_TRICKS_ITEM, create_regions, can_reach_room
from . import tracker_map, tracker_route
from .client import CVAOSClient  # noqa: F401  (imported for its registration side effect)


def launch_collection_client(*args: str) -> None:
    from .collection_client import launch
    launch_component(launch, name="CVAoSCollectionClient", args=args)


def launch_collection_installer(*args: str) -> None:
    # Runs in the Launcher process (Messenger-style tool component): prompts for the
    # patched .gba, resolves game.exe from settings, swaps the ROM into windata/.
    from .advance_collection.install import run_from_launcher
    run_from_launcher(*args)


def launch_collection_restore(*args: str) -> None:
    # Runs in the Launcher process, like the installer: puts the stock archive back from the
    # installer's backups.
    from .advance_collection.install import run_from_launcher
    run_from_launcher("--restore", *args)


# The BizHawk client auto-registers via its patch_suffix; the Advance Collection client
# has no patch file to open, so it needs an explicit Launcher button.
components.append(Component(
    "CVAoS Collection Client",
    func=launch_collection_client,
    component_type=Type.CLIENT,
    description="Play a CVAoS seed inside the Steam Castlevania Advance Collection.",
))

components.append(Component(
    "CVAoS Collection ROM Installer",
    func=launch_collection_installer,
    component_type=Type.TOOL,
    description="Install a CVAoS seed into the Steam Castlevania Advance Collection: accepts "
                "the .apcvaos patch (applied for you) or a patched .gba. Backs up the original "
                "archive; restore any time.",
))

components.append(Component(
    "CVAoS Collection Restore",
    func=launch_collection_restore,
    component_type=Type.TOOL,
    description="Put the Steam Castlevania Advance Collection's stock game files back from the "
                "backups the CVAoS Collection ROM Installer made.",
))


class CVAOSSettings(settings.Group):
    class RomFile(settings.UserFilePath):
        """The base ROM for a **gba**-target seed. Prompted for when applying the patch; you may
        select a Castlevania AoS (US) GBA ROM *or* the Advance Collection's game.exe -- if you
        pick the exe, the base ROM is extracted from the collection for you. (advance_collection
        seeds ignore this and source the ROM from collection_exe automatically.)"""
        description = "Castlevania AoS (US) ROM, or Advance Collection game.exe"
        # Accept a GBA ROM (cart or collection-extracted) or the collection exe. No copy_to:
        # if the user hands us the exe, it must stay next to its windata/ so we can extract.
        md5s = [USA_ROM_MD5, AC_USA_ROM_MD5, AC_GAME_EXE_MD5]

        def browse(self, **kwargs):  # type: ignore[override]
            # Let the picker show both the ROM and the exe (default would filter to *.gba only).
            kwargs.setdefault("filetypes", [("AoS ROM or Collection game.exe", [".gba", ".exe"])])
            return super().browse(**kwargs)

    class CollectionExePath(settings.FilePath):
        """game.exe inside the Steam Castlevania Advance Collection install. Used to install a
        patched ROM into the collection, and to source the base ROM for advance_collection-target
        seeds. Optional: defaults to the standard Steam path, so gba-only users can ignore it. The
        ROM installer asks for it when nothing is at that path, and checks the chosen file's md5."""
        description = "Castlevania Advance Collection game.exe"
        is_exe = True
        required = False
        md5s = [AC_GAME_EXE_MD5]

    rom_file: RomFile = RomFile("Castlevania - Aria of Sorrow (USA).gba")
    collection_exe: CollectionExePath = CollectionExePath(AC_DEFAULT_EXE_PATH)


class CVAOSWebWorld(WebWorld):
    theme = "ice"

    option_groups = cvaos_option_groups

    setup_en = Tutorial(
        "Multiworld Setup Guide",
        "A guide to setting up CVAoS in Archipelago.",
        "English",
        "setup_en.md",
        "setup/en",
        ["dem1995"]
    )

    tutorials = [setup_en]


class CVAOSWorld(World):
    """
    Castlevania: Aria of Sorrow is a 2003 action-adventure game developed by Konami
    for the Game Boy Advance. Play as Soma Cruz and explore Dracula's castle,
    collecting souls from defeated enemies to gain their abilities.
    """

    game = "Castlevania - Aria of Sorrow"
    web = CVAOSWebWorld()

    options_dataclass = CVAOSOptions
    options: CVAOSOptions
    settings: ClassVar[CVAOSSettings]

    item_name_to_id = item_name_to_id
    location_name_to_id = location_name_to_id

    topology_present = True
    origin_region_name = "Menu"

    # Both set in create_regions: enemy_number -> region name, and room id -> its door
    # region names. Used by the goal's reachability checks (regions.can_reach_*).
    enemy_region_name_by_number: dict[int, str]
    entrance_region_names_by_room: dict[str, list[str]]

    # Per-slot authentication: baked into the ROM (rom/patch.py) and registered under
    # connect_names (modify_multidata) so the client can connect by reading it from the ROM.
    auth: bytearray

    # Universal Tracker rebuilds this world from the seed's slot_data, without the player's YAML. It
    # passes slot_data through interpret_slot_data and stores the result in
    # multiworld.re_gen_passthrough. It then runs the generation steps up to generate_basic, and
    # generate_early reads the options back from the passthrough. The YAML-less choice is made for
    # the whole world, so it applies to older seeds too (see interpret_slot_data).
    ut_can_gen_without_yaml = True

    # Universal Tracker shows the locations that open up once it collects this item as reachable
    # with tricks, and only the tracker's rebuild of the world has rules that use it.
    glitches_item_name = UT_TRICKS_ITEM

    # Universal Tracker's map tab. The maps, their images and the pin for each location are all
    # generated from this world's data (tracker_map.py builds them into tracker/), so no game art
    # ships and nothing else needs installing. Soma's marker and the choice of map follow the
    # position the client keeps in data storage.
    tracker_world: ClassVar[dict] = {
        "map_page_folder": tracker_map.FOLDER,
        "map_page_maps": [tracker_map.MAPS_FILE],
        "map_page_locations": [tracker_map.LOCATIONS_FILE],
        "map_page_groups": tracker_map.MAP_GROUPS,
        "map_page_setting_key": tracker_map.POSITION_KEY,
        "map_page_index": tracker_map.map_page_index,
        "location_setting_key": tracker_map.POSITION_KEY,
        "location_icon_coords": tracker_map.position_icon,
    }

    def get_logical_path(self, dest_name: str, state: CollectionState):
        """Routes list the rooms on the way, area by area, with the souls or weapons each room
        needs. They lead to locations; for a region or an entrance, UT shows its own route."""
        return tracker_route.describe_route(self, dest_name, state)

    @staticmethod
    def interpret_slot_data(slot_data: dict) -> dict:
        """Return ``slot_data`` unchanged, so Universal Tracker rebuilds the world from it.

        A seed generated before slot_data carried every logic option is still rebuilt, and the
        missing options take their defaults. A warning names them, because the tracker's logic can
        then differ from the seed's.
        """
        missing = [name for name in LOGIC_OPTION_NAMES if name not in slot_data]
        if missing:
            logging.getLogger("Client").warning(
                "This Aria of Sorrow seed was generated before its slot data recorded every logic "
                "option, so the tracker assumes the default for: %s.", ", ".join(missing))
        return slot_data
    # Set in create_items: the items that item smoothing places in pre_fill, kept out of the
    # multiworld pool (item_smoothing.take_smoothing_items).
    _smoothing_items: Sequence[Item] = ()

    def generate_early(self) -> None:
        self._restore_logic_options_for_tracker()
        self.auth = bytearray(self.random.getrandbits(8) for _ in range(16))

    def _restore_logic_options_for_tracker(self) -> None:
        """When Universal Tracker regenerates this world, take the logic options from slot_data.

        slot_data from a seed generated before an option was added to LOGIC_OPTION_NAMES has no
        key for it, so that option keeps the value the rebuilt world started with.
        """
        passthrough = getattr(self.multiworld, "re_gen_passthrough", {}).get(self.game)
        if not passthrough:
            return
        for name in LOGIC_OPTION_NAMES:
            if name in passthrough:
                option_type = type(getattr(self.options, name))
                setattr(self.options, name, option_type.from_any(passthrough[name]))

    def create_regions(self) -> None:
        create_regions(self)
        player = self.player
        if self.options.goal == Goal.option_graham:
            # Bad ending: defeat Graham (reach his room, 904).
            self.multiworld.completion_condition[player] = \
                lambda state: can_reach_room(state, self, "904")
        else:
            # True ending: reach the Chaos arena (room B14). The Chaotic Realm is reachable
            # only through the gated 507 -> 506 door, which already requires the Flame Demon,
            # Succubus, and Giant Bat souls plus beating Graham, so reaching B14 encodes the
            # whole true-ending requirement (see regions._chaotic_realm_gate).
            self.multiworld.completion_condition[player] = \
                lambda state: can_reach_room(state, self, "B14")

    def create_items(self) -> None:
        from .item_smoothing import take_smoothing_items
        itempool = create_itempool(self)
        # Hold back the items smoothing places in pre_fill, because the pool must not change after
        # create_items.
        self._smoothing_items = take_smoothing_items(self, itempool)
        self.multiworld.itempool += itempool

    def get_pre_fill_items(self) -> list[Item]:
        # Return the held-back smoothing items that pre_fill has not placed yet, so get_all_state
        # still collects them.
        return [item for item in self._smoothing_items if item.location is None]

    def create_item(self, name: str) -> CVAOSItem:
        if name == UT_TRICKS_ITEM:
            return CVAOSItem(name, ItemClassification.progression, None, self.player)
        return create_item(self, name)

    def collect(self, state: CollectionState, item: Item) -> bool:
        # Collecting a Progressive Movement copy also adds the next stage soul to the state. The
        # access rules ask for Flying Armor, Malphas or Giant Bat by name, so they work unchanged.
        # With the option off, the client grants no soul for a copy (one put in the starting
        # inventory or a plando, say), so logic counts none either.
        changed = super().collect(state, item)
        if changed and item.name == PROGRESSIVE_MOVEMENT and self.options.progressive_movement:
            stage = state.count(PROGRESSIVE_MOVEMENT, self.player)
            if stage <= len(PROGRESSIVE_MOVEMENT_STAGES):
                state.add_item(PROGRESSIVE_MOVEMENT_STAGES[stage - 1], self.player)
        return changed

    def remove(self, state: CollectionState, item: Item) -> bool:
        stage = state.count(PROGRESSIVE_MOVEMENT, self.player)
        changed = super().remove(state, item)
        if (changed and item.name == PROGRESSIVE_MOVEMENT and self.options.progressive_movement
                and 0 < stage <= len(PROGRESSIVE_MOVEMENT_STAGES)):
            state.remove_item(PROGRESSIVE_MOVEMENT_STAGES[stage - 1], self.player)
        return changed

    def pre_fill(self) -> None:
        # Logic-aware placement before the main fill, gated on item_smoothing ("off" keeps vanilla):
        #  - bias the gating mobility souls (Malphas or Hippogryph early; Giant Bat late),
        #  - spread the Ancient Books across early/mid/late (rough pass; refined post-fill, and
        #    logic-safe even once the books are required to reach Graham for the good ending), and
        #  - put anything those passes could not place into the pool for the main fill.
        if self.options.item_smoothing.current_key != "off":
            from .item_smoothing import (add_unplaced_to_pool, bias_progression_souls,
                                         spread_ancient_books)
            bias_progression_souls(self)
            spread_ancient_books(self)
            add_unplaced_to_pool(self)

    def get_filler_item_name(self) -> str:
        # Item links and fill fallbacks create filler through this. The default picks any item
        # name, which here includes progression souls and items that exist only in Hard Mode.
        return self.random.choice(FILLER_ITEM_NAMES)

    @classmethod
    def stage_post_fill(cls, multiworld: MultiWorld) -> None:
        # First refine the gating mobility souls and the Ancient Books to their target run-positions
        # (verified-safe swaps), then re-order placed non-progression items -- plus the ceiling/floor
        # breakers -- so weaker items come earlier and stronger later (smooth_placed_items keeps the
        # breakers logic-safe). Runs once for all cvaos slots; no-op for any slot with smoothing off.
        from .item_smoothing import (refine_ancient_books, refine_soul_positions,
                                     smooth_placed_items)
        for world in multiworld.get_game_worlds(cls.game):
            if world.options.item_smoothing.current_key != "off":
                refine_soul_positions(world)
                refine_ancient_books(world)
        smooth_placed_items(multiworld, cls.game)

    def generate_output(self, output_directory: str) -> None:
        from .rom import CVAOSProcedurePatch, get_location_data, patch_rom

        active_locations = [loc for loc in self.multiworld.get_locations(self.player)
                            if loc.address is not None]
        offset_data = get_location_data(self, active_locations)

        patch = CVAOSProcedurePatch(player=self.player, player_name=self.player_name)
        patch_rom(self, patch, offset_data)

        rom_path = os.path.join(
            output_directory,
            f"{self.multiworld.get_out_file_name_base(self.player)}{patch.patch_file_ending}")
        patch.write(rom_path)

    def fill_slot_data(self) -> dict:
        return {
            "target_platform": self.options.target_platform.value,
            "randomize_pickups": self.options.randomize_pickups.value,
            "skull_key_warp": self.options.skull_key_warp.value,
            "new_soul_pause": self.options.new_soul_pause.value,
            "death_link": self.options.death_link.value,
            **self.options.as_dict(*LOGIC_OPTION_NAMES),
            # Locations holding this world's own Progressive Movement copies, and how many copies
            # the starting inventory holds. The client adds the checked locations and the starting
            # copies to the copies received from other worlds, and grants stage souls up to that
            # total, the same count logic uses.
            "progressive_movement_starting": sum(
                1 for item in self.multiworld.precollected_items[self.player]
                if item.name == PROGRESSIVE_MOVEMENT),
            "progressive_movement_locations": sorted(
                loc.address for loc in self.multiworld.get_locations(self.player)
                if loc.address is not None and loc.item is not None
                and loc.item.player == self.player and loc.item.name == PROGRESSIVE_MOVEMENT),
            # The client receives the starting inventory and counts it in the saved received
            # counter.
            # Seeds without this key were generated for a client that did neither.
            "starting_inventory_from_server": True,
        }

    def modify_multidata(self, multidata: dict) -> None:
        # Register the slot's auth so the client can connect by it (read from the ROM).
        new_name = base64.b64encode(self.auth).decode("ascii")
        multidata["connect_names"][new_name] = multidata["connect_names"][self.player_name]
