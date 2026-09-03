"""Constants shared between generation, ROM patching, and the client."""

# MD5 of the clean Castlevania: Aria of Sorrow (USA) ROM, lowercase hex.
#
# Kept as a string rather than an int because both consumers want this exact text: the
# settings group offers it as ``md5s`` when prompting for the ROM, and the procedure patch
# compares it against the source file. Formatting an int would also drop a leading zero.
USA_ROM_MD5 = "e7470df4d241f73060d14437011b90ce"

# MD5 of the same ROM as shipped inside the Steam Castlevania Advance Collection. M2's copy
# carries its own additions (see ``M2_NO_GO`` in rom/address_space.py), so it hashes
# differently from a cart dump. Accepted as a patch base alongside the cart hash.
AC_USA_ROM_MD5 = "899b136ff8391f574b52be34ddb683fc"

# MD5 of the collection's game.exe (only one build was ever shipped). The settings group
# hash-validates the path the user browses to, and the installer locates windata/ next to it.
AC_GAME_EXE_MD5 = "3ca8f446f8a63e281d4c125d79b103bb"
AC_DEFAULT_EXE_PATH = ("C:/Program Files (x86)/Steam/steamapps/common/"
                       "Castlevania Advance Collection/game.exe")
