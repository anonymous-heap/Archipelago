"""Constants shared between generation, ROM patching, and the client."""

# MD5 of the clean Castlevania: Aria of Sorrow (USA) ROM, lowercase hex.
#
# Kept as a string rather than an int because both consumers want this exact text: the
# settings group offers it as ``md5s`` when prompting for the ROM, and the procedure patch
# compares it against the source file. Formatting an int would also drop a leading zero.
USA_ROM_MD5 = "e7470df4d241f73060d14437011b90ce"
