# Soul tier lists from TierMaker

Community soul rankings are often made with TierMaker, which shows each soul as a picture. This
folder records which soul each picture shows, and holds rankings decoded from such lists.

| Path | Contents |
| --- | --- |
| `template_images.csv` | Which soul and enemy each image of the main template shows. |
| `template_images_17251266.csv` | Which soul each image of the second template shows. |
| `lists/` | Rankings decoded from lists, in the format of `../tier_jsons/`. |

`../build_soul_tiers.py` reads `lists/` to rate each soul in `../soul_tiers.csv`.
`../aggregate_tiers.py` reads only `../tier_jsons/`, so nothing here changes
`../desirability.csv` until a list is copied there.

The template images themselves are not kept in the repository. The CSVs name each image by its
TierMaker file name, and TierMaker's image list for a template (see "Reading a list") gives their
location.

## The templates

The **main template** is "Castlevania Aria of Sorrow Red/Blue/Yellow Souls" (template id
`castlevania-aria-of-sorrow-red-blue-yellow-souls-1207376`, last edited 2022-09-24). It has 114
images, one for each red, blue and yellow soul.

The **second template** is "Castlevania - Aria of Sorrow: Tactical Soul tierlist" (template id
`castlevania---aria-of-sorrow-tactical-soul-tierlist-17251266`, last edited 2024-06-22). It holds
two sets of images:

- The 108 **plain** images resemble the main template's.
- The 111 **textured** images, whose file names start with `zzzzz-`, show each enemy at game size
  on a dark stone background. This set adds Kicker Skeleton but lacks Black Panther, Flying
  Armor, Skula and Undine, so a list made from it can't rank those four.

## `template_images.csv`

| Column | Meaning |
| --- | --- |
| `template_index` | The image's position in the template, counting from 1. List pages refer to images by this number. |
| `image_file` | The image's file name on TierMaker. |
| `soul` | The soul, named as in `../../item_info/item_info.csv`. |
| `soul_color` | `red`, `blue` or `yellow`. |
| `enemy` | The enemy that drops the soul, named as the game names it. |
| `enemy_id` | That enemy's id in the game: its row in the enemy stat table at GBA 0x080E9644. |
| `note` | Set only for the five souls described next. |

No enemy drops Flying Armor, Giant Bat, Black Panther, Undine or Skula. They are ground pickups,
so their `enemy` is empty. Giant Bat's image is a bat, and the other four show Soma using the
soul.

The soul names come from the file names, such as `aosbasiliskpng.png`. Each image was also
checked by eye against the soul it is matched to.

`template_images_17251266.csv` has the same first three columns, plus `image_set`, which is
`plain` or `textured`.

## Reading a list

**From its page.** A list's page holds the ranking as text, in its call to
`tierSystem.initList`:

    Elite|0|8|83|15|82|91|74==Excellent|1|107|23|...

Tiers are separated by `==`. Each tier gives its label, then a color number, then the
`template_index` of each image in it, in order. Decoding this is exact.

The template's image list comes from `https://tiermaker.com/api/?type=templates-v2&id=<template id>`.
Its first entry is the image folder's path and the images follow, which is why the indexes count
from 1.

**From a picture.** A finished list is drawn in one of two layouts:

- By default, each image is cropped to a square around its center, so a tall or wide image shows
  only its middle.
- A list can instead keep each image's proportions. The lists made from the textured set do this,
  so their tiles differ in width.

Two souls can't be told apart in a square tile: Killer Doll and Lightning Doll share a body, and
the crop removes the hair and boots that differ. This is true of both templates' plain images.
Many other enemies are palette swaps of each other, such as Blue Crow and Red Crow or Minotaur and
Red Minotaur, and in a picture they differ only in color.

## `lists/`

Each tier lists its souls in the order the rater placed them. A few raters ordered souls within a
tier, as sfuth's "S (Vaguely Ordered)" says, but most did not.

| File | Rater and source | Template and layout | Souls ranked |
| --- | --- | --- | --- |
| `backJramhall_red_souls.json` | "Aria Red Souls" by backJramhall, 2022-09-24, decoded from its page ([list 2481272](https://tiermaker.com/list/video-games/castlevania-aria-of-sorrow-red-blue-yellow-souls-1207376/2481272)) | Main | 55: the red souls only |
| `berix.json` | berix, from a picture of the list | Second, textured, proportional | 111: the whole textured set |
| `jammer.json` | jammer, from a picture of the list | Main, square | 114: every soul |
| `ruleroftri.json` | ruleroftri, from a picture of the list | Main, square | 110: all but Flying Armor, Skula, Undine and Sky Fish |
| `sfuth.json` | sfuth, from a picture of the list | Second, textured, proportional | 111: the whole textured set |
| `vaati.json` | vaati, from a picture of the list | Main, square | 109: all but Flying Armor, Giant Bat, Skula, Undine and Killer Doll |

The pictures were read by matching each tile to the template image it differs from least, pixel
by pixel. Every match was then checked by eye against its image. The pictures themselves are not
kept here.

Which doll is which in jammer's, ruleroftri's and vaati's lists is inferred, because their square
tiles show the dolls identically. Jammer and ruleroftri each rank one doll in S and one in C, and
vaati ranks a single doll in its second tier. Every list that tells the dolls apart ranks Lightning
Doll higher: backJramhall puts it in Elite and Killer Doll in Good, berix in its 2nd and 6th of 11
tiers, and sfuth in S and C. So the higher doll in each of the three lists is recorded as
Lightning Doll, and vaati's list is taken to leave out Killer Doll.
