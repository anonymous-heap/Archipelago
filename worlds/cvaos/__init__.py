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
                    item_name_groups, item_name_to_id, create_item, create_itempool)
from .enemy_checks import defeat_location_name, enemy_check_types, has_recorded_route
from .locations import (defeat_hint_text, location_name_groups, location_name_to_id,
                        pickup_location_ids)
from .options import (LOGIC_OPTION_NAMES, CVAOSOptions, Goal, SoulShuffle, cvaos_option_groups,
                      cvaos_options_presets)
from .regions import UT_TRICKS_ITEM, create_regions, can_reach_room
from . import tracker_map, tracker_route
from .client import CVAOSClient  # noqa: F401  (imported for its registration side effect)


def launch_collection_client(*args: str) -> None:
    from .collection_client import launch
    launch_component(launch, name="CVAoSCollectionClient", args=args)


def launch_tracker(*args: str) -> None:
    from .tracker_app import launch
    launch_component(launch, name="CVAoSTracker", args=args)


def launch_collection_installer(*args: str) -> None:
    # Runs in the Launcher process, as a tool component. It finds game.exe through the settings
    # and asks for an .apcvaos patch or a patched .gba, unless one was passed in. It then installs
    # the ROM into the collection's windata/.
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

# When the Launcher opens an AP room's archipelago:// link, it offers the tracker too, with the
# link's server address and slot name filled in.
components.append(Component(
    "Aria of Sorrow Tracker",
    func=launch_tracker,
    component_type=Type.CLIENT,
    game_name="Castlevania - Aria of Sorrow",
    supports_uri=True,
    description="A map of the castle in your browser: what is in logic, where Soma is, and the "
                "way to any location.",
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
        """The base ROM for a gba-target seed, asked for when a patch is first applied. Select a
        Castlevania AoS (US) GBA ROM. The Advance Collection's game.exe also works, and the ROM is
        then extracted from the collection, but that ROM has no sound outside the collection. An
        advance_collection seed uses the installed collection's ROM instead, and falls back to
        this file only when it can't find the collection."""
        description = "Castlevania AoS (US) ROM, or Advance Collection game.exe"
        # Accept a GBA ROM (cart or collection-extracted) or the collection exe. No copy_to:
        # if the user hands us the exe, it must stay next to its windata/ so we can extract.
        md5s = [USA_ROM_MD5, AC_USA_ROM_MD5, AC_GAME_EXE_MD5]

        def browse(self, **kwargs):  # type: ignore[override]
            # Let the picker show both the ROM and the exe (default would filter to *.gba only).
            kwargs.setdefault("filetypes", [("AoS ROM or Collection game.exe", [".gba", ".exe"])])
            return super().browse(**kwargs)

    # A user path, like rom_file, so that a game.exe picked inside the Archipelago folder is saved
    # relative to that folder and read back from it, whatever the working directory.
    class CollectionExePath(settings.UserFilePath):
        """game.exe inside the Steam Castlevania Advance Collection install. Used to install a
        patched ROM into the collection, and to source the base ROM for advance_collection-target
        seeds. Optional: defaults to the standard Steam path, so gba-only users can ignore it.
        Only a file with the collection's md5 is used. When the file at this path is missing or
        has another md5, the collection is looked for in every Steam library, and the ROM
        installer asks for it only if that fails."""
        description = "Castlevania Advance Collection game.exe"
        is_exe = True
        required = False
        md5s = [AC_GAME_EXE_MD5]

    rom_file: RomFile = RomFile("Castlevania - Aria of Sorrow (USA).gba")
    collection_exe: CollectionExePath = CollectionExePath(AC_DEFAULT_EXE_PATH)


class CVAOSWebWorld(WebWorld):
    theme = "ice"
    bug_report_page = "https://github.com/anonymous-heap/Archipelago/issues"

    option_groups = cvaos_option_groups
    options_presets = cvaos_options_presets

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
    item_name_groups = item_name_groups
    location_name_to_id = location_name_to_id
    location_name_groups = location_name_groups

    topology_present = True
    origin_region_name = "Menu"

    # Both set in create_regions: enemy_number -> region name, and room id -> its door
    # region names. The regions.can_reach_* helpers read them for the goal, the Chaotic Realm gate,
    # the transformation-soul (Tform) requirement, and enemy checks that need a boss beaten first.
    enemy_region_name_by_number: dict[int, str]
    entrance_region_names_by_room: dict[str, list[str]]

    # Per-slot authentication: baked into the ROM (rom/patch.py) and registered under
    # connect_names (modify_multidata) so the client can connect by reading it from the ROM.
    auth: bytearray

    # Universal Tracker rebuilds this world from the seed's slot data, without the player's YAML. It
    # passes the slot data through interpret_slot_data and stores the result in
    # multiworld.re_gen_passthrough. It then runs the generation steps through generate_basic with
    # default options, except that generate_early sets the logic options from the passthrough.
    # The flag applies to every seed of this game, so an older seed whose slot data lacks some
    # logic options is rebuilt too (see interpret_slot_data).
    ut_can_gen_without_yaml = True

    # Universal Tracker shows a location as reachable with tricks when it becomes reachable once UT
    # collects this item. Only UT's rebuild of the world has rules that use the item.
    glitches_item_name = UT_TRICKS_ITEM

    # Universal Tracker's map tab. tracker_map.py generates the maps, their images and each
    # location's pin from this world's data into tracker/, so no game art ships and nothing else
    # needs installing. Soma's marker and the choice of map follow the map cell the client keeps
    # in data storage.
    tracker_world: ClassVar[dict] = {
        "map_page_folder": tracker_map.FOLDER,
        "map_page_maps": [tracker_map.MAPS_FILE],
        "map_page_locations": [tracker_map.LOCATIONS_FILE],
        "map_page_groups": tracker_map.MAP_GROUPS,
        "map_page_setting_key": tracker_map.MAP_CELL_KEY,
        "map_page_index": tracker_map.map_page_index,
        "location_setting_key": tracker_map.MAP_CELL_KEY,
        "location_icon_coords": tracker_map.position_icon,
    }

    def get_logical_path(self, dest_name: str, state: CollectionState):
        """Return a Universal Tracker route to the location named ``dest_name``, or None.

        The route lists the rooms on the way, one line per area, and names what each room needs.
        Only a location gets a route here. For a region or an entrance this returns None, and UT
        shows its own route instead.
        """
        return tracker_route.describe_route(self, dest_name, state)

    @staticmethod
    def interpret_slot_data(slot_data: dict) -> dict:
        """Return ``slot_data`` unchanged, so Universal Tracker rebuilds the world from it.

        A seed generated before its slot data carried every logic option is still rebuilt, and the
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

    # At most this many defeat checks without a recorded route may be priority locations. Such a
    # check requires all eight fallback souls, and the fill places progression on priority
    # locations first. The souls must then go where none of the progression on those checks is
    # needed, and with many such checks they run out of places. Seeds stop generating reliably with
    # 25 or more, as measured in local_docs/ENEMY_CHECKS_DESIGN.md.
    MAX_FALLBACK_PRIORITY = 10

    def generate_early(self) -> None:
        self._restore_logic_options_for_tracker()
        self.auth = bytearray(self.random.getrandbits(8) for _ in range(16))
        self._cap_fallback_priority_locations()
        self._set_effective_values()

    def _set_effective_values(self) -> None:
        """Turn off the options that a newer option replaces, so the spoiler shows what runs.

        Enemy Drop Rate above 100 replaces Multiply Soul Drop Rates, and Soul Shuffle's matched
        keeps each enemy's own soul drop rate, whatever Keep Soul Drop Rates says. Each option
        turned off logs one line.
        """
        options = self.options
        if options.enemy_drop_rate.value > 100 and options.multiply_soul_drop_rates:
            options.multiply_soul_drop_rates.value = 0
            logging.info("%s: Multiply Soul Drop Rates is off, because Enemy Drop Rate %d replaces "
                         "it.", self.player_name, options.enemy_drop_rate.value)
        if options.soul_shuffle == SoulShuffle.option_matched and options.keep_soul_drop_rates:
            options.keep_soul_drop_rates.value = 0
            logging.info("%s: Keep Soul Drop Rates is off, because Soul Shuffle's matched keeps "
                         "each enemy's own soul drop rate.", self.player_name)

    def _cap_fallback_priority_locations(self) -> None:
        """Keep at most MAX_FALLBACK_PRIORITY defeat checks without a route as priority locations.

        The rest become ordinary locations, and a warning names them.
        """
        fallback_only = sorted(defeat_location_name(enemy.name)
                               for enemy in enemy_check_types(self.options)
                               if not has_recorded_route(enemy.name))
        priority = self.options.priority_locations.value
        demoted = [name for name in fallback_only if name in priority][self.MAX_FALLBACK_PRIORITY:]
        if demoted:
            priority.difference_update(demoted)
            logging.warning(
                "%s: at most %d enemy checks without a recorded route can be priority locations, so "
                "these are ordinary locations instead: %s", self.player_name,
                self.MAX_FALLBACK_PRIORITY, ", ".join(demoted))

    def _restore_logic_options_for_tracker(self) -> None:
        """Set the logic options from the slot data when Universal Tracker rebuilds this world.

        The slot data of a seed generated before an option joined LOGIC_OPTION_NAMES has no key for
        that option, so the option keeps its default. The slot data of a seed generated by a newer
        version of this world can hold an option value that this version doesn't know. That option
        also keeps its default, and a warning is logged instead of the rebuild failing.
        """
        passthrough = getattr(self.multiworld, "re_gen_passthrough", {}).get(self.game)
        if not passthrough:
            return
        for name in LOGIC_OPTION_NAMES:
            if name in passthrough:
                option_type = type(getattr(self.options, name))
                try:
                    setattr(self.options, name, option_type.from_any(passthrough[name]))
                except (KeyError, ValueError, TypeError):
                    logging.getLogger("Client").warning(
                        "This Aria of Sorrow seed's %s is %r, which this version doesn't know, so "
                        "the tracker assumes the default.", name, passthrough[name])

    def create_regions(self) -> None:
        create_regions(self)
        player = self.player
        if self.options.goal == Goal.option_graham:
            # Bad ending: defeat Graham (reach his room, 904).
            self.multiworld.completion_condition[player] = \
                lambda state: can_reach_room(state, self, "904")
        else:
            # True ending: reach the Chaos arena (room B14). The Chaotic Realm is reachable
            # only through the gated 507 -> 506 door. That door already requires the three Ancient
            # Books, the Giant Bat soul and beating Graham, and it requires reaching a Flame Demon
            # and a Succubus for their souls. So reaching B14 covers the whole true-ending
            # requirement (see regions._chaotic_realm_gate).
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
        # With item smoothing on, some progression is placed before the main fill, and only where
        # logic can reach it. bias_progression_souls places the gating mobility souls and Black
        # Panther near targets in the run, following Early Mobility Souls and Black Panther Bias.
        # spread_ancient_books places the Ancient Books in early, middle and late bands of map
        # depth, each where it is reachable without any book. stage_post_fill refines both.
        # Anything these passes could not place goes into the pool for the main fill.
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
        # Runs once for all cvaos slots, and skips any slot with item smoothing off. It first moves
        # the souls that pre_fill biased and the Ancient Books closer to their target positions in
        # the run, keeping only the swaps that leave the multiworld's accessibility intact. It then
        # reorders the placed non-progression items and the ceiling and floor breakers, so weaker
        # items come earlier and stronger ones later. If that order breaks accessibility,
        # smooth_placed_items puts the breakers back where the main fill placed them.
        from .item_smoothing import (refine_ancient_books, refine_soul_positions,
                                     smooth_placed_items)
        for world in multiworld.get_game_worlds(cls.game):
            if world.options.item_smoothing.current_key != "off":
                refine_soul_positions(world)
                refine_ancient_books(world)
        smooth_placed_items(multiworld, cls.game)

    def extend_hint_information(self, hint_data: dict[int, dict[int, str]]) -> None:
        # A hint for a defeat location names the rooms where its enemy appears. The text depends
        # only on the enemy, never on where items were placed.
        hints = {location_name_to_id[defeat_location_name(enemy.name)]: defeat_hint_text(enemy.name)
                 for enemy in enemy_check_types(self.options)}
        if hints:
            hint_data.setdefault(self.player, {}).update(hints)

    def generate_output(self, output_directory: str) -> None:
        from .rom import CVAOSProcedurePatch, get_location_data, patch_rom

        # Only pickups are written into the ROM. A defeat location has no ROM pickup.
        active_locations = [loc for loc in self.multiworld.get_locations(self.player)
                            if loc.address in pickup_location_ids]
        offset_data = get_location_data(self, active_locations)

        patch = CVAOSProcedurePatch(player=self.player, player_name=self.player_name)
        patch_rom(self, patch, offset_data)

        rom_path = os.path.join(
            output_directory,
            f"{self.multiworld.get_out_file_name_base(self.player)}{patch.patch_file_ending}")
        patch.write(rom_path)

    def fill_slot_data(self) -> dict:
        from .rom.patch import rom_gives_item

        return {
            "target_platform": self.options.target_platform.value,
            "randomize_pickups": self.options.randomize_pickups.value,
            "skull_key_warp": self.options.skull_key_warp.value,
            "new_soul_pause": self.options.new_soul_pause.value,
            "death_link": self.options.death_link.value,
            **self.options.as_dict(*LOGIC_OPTION_NAMES),
            # Locations holding this world's own Progressive Movement copies, and how many copies
            # the starting inventory holds. The client adds up the starting copies, the copies
            # received from other worlds, and how many of these locations are checked. It grants
            # stage souls up to that total, which is the same count logic uses.
            "progressive_movement_starting": sum(
                1 for item in self.multiworld.precollected_items[self.player]
                if item.name == PROGRESSIVE_MOVEMENT),
            "progressive_movement_locations": sorted(
                loc.address for loc in self.multiworld.get_locations(self.player)
                if loc.address is not None and loc.item is not None
                and loc.item.player == self.player and loc.item.name == PROGRESSIVE_MOVEMENT),
            # The client receives the starting inventory from the server and counts it in the
            # received-item counter it saves. Seeds without this key were generated for a client
            # that did neither.
            "starting_inventory_from_server": True,
            # The client asks the server for this player's own items too, because the ROM never
            # hands out the item at a defeat location. It skips the items the ROM already handed
            # out: those at these locations.
            "rom_given_locations": sorted(
                loc.address for loc in self.multiworld.get_locations(self.player)
                if loc.address in pickup_location_ids and loc.item is not None
                and rom_gives_item(self, loc)),
        }

    def modify_multidata(self, multidata: dict) -> None:
        # Register the slot's auth so the client can connect by it (read from the ROM).
        new_name = base64.b64encode(self.auth).decode("ascii")
        multidata["connect_names"][new_name] = multidata["connect_names"][self.player_name]
