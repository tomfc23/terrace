// Generated from the written STLs — ids, sizes and masses are read back off disk,
// not copied from a build log. Regenerate by rebuilding with pitch_print.py and
// re-running the manifest step; each design keeps its own id, file and thumbnail.
export const DESIGNS = [
  {
    "id": "baseline",
    "title": "The baseline",
    "family": "smoothing",
    "lossy": false,
    "note": "Your pick: 12 x 8 cells, sigma 4 m, nothing else touched",
    "flags": "--grid 12x8 --sigma 4",
    "file": "../models/design_baseline.stl",
    "thumb": "../plots/design_baseline_print.png",
    "tris": 464,
    "size": [
      180.0,
      116.6,
      12.4
    ],
    "vol": 105.4,
    "grams": 131
  },
  {
    "id": "tight_sigma2",
    "title": "Tight pencil",
    "family": "smoothing",
    "lossy": false,
    "note": "Smoothing tighter than a cell, so peaks stand alone",
    "flags": "--grid 12x8 --sigma 2",
    "file": "../models/design_tight_sigma2.stl",
    "thumb": "../plots/design_tight_sigma2_print.png",
    "tris": 464,
    "size": [
      180.0,
      116.6,
      11.5
    ],
    "vol": 98.1,
    "grams": 122
  },
  {
    "id": "wide_sigma8",
    "title": "Broad swell",
    "family": "smoothing",
    "lossy": false,
    "note": "Smoothing wider than two cells fuses it into one swell",
    "flags": "--grid 12x8 --sigma 8",
    "file": "../models/design_wide_sigma8.stl",
    "thumb": "../plots/design_wide_sigma8_print.png",
    "tris": 464,
    "size": [
      180.0,
      116.6,
      14.0
    ],
    "vol": 123.5,
    "grams": 153
  },
  {
    "id": "facets_18x12",
    "title": "Bigger facets",
    "family": "facets",
    "lossy": false,
    "note": "Bigger, cleaner facets, a little less interpolation",
    "flags": "--grid 18x12 --sigma 3.5",
    "file": "../models/design_facets_18x12.stl",
    "thumb": "../plots/design_facets_18x12_print.png",
    "tris": 984,
    "size": [
      180.0,
      116.6,
      12.2
    ],
    "vol": 93.0,
    "grams": 115
  },
  {
    "id": "facets_24x16",
    "title": "Medium facets",
    "family": "facets",
    "lossy": false,
    "note": "Medium facets, the resolution the data nearly supports",
    "flags": "--grid 24x16 --sigma 3",
    "file": "../models/design_facets_24x16.stl",
    "thumb": "../plots/design_facets_24x16_print.png",
    "tris": 1696,
    "size": [
      180.0,
      116.6,
      12.6
    ],
    "vol": 90.5,
    "grams": 112
  },
  {
    "id": "facets_36x23",
    "title": "Fine facets",
    "family": "facets",
    "lossy": false,
    "note": "Fine facets; the speckle is resolution, not the player",
    "flags": "--grid 36x23 --sigma 2.5",
    "file": "../models/design_facets_36x23.stl",
    "thumb": "../plots/design_facets_36x23_print.png",
    "tris": 3548,
    "size": [
      180.0,
      116.6,
      12.1
    ],
    "vol": 78.8,
    "grams": 98
  },
  {
    "id": "levels_12",
    "title": "Contour bands",
    "family": "stepping",
    "lossy": true,
    "note": "Twelve bands: nearly lossless stepping",
    "flags": "--grid 12x8 --sigma 4 --levels 12",
    "file": "../models/design_levels_12.stl",
    "thumb": "../plots/design_levels_12_print.png",
    "tris": 464,
    "size": [
      180.0,
      116.6,
      12.3
    ],
    "vol": 105.6,
    "grams": 131
  },
  {
    "id": "levels_6",
    "title": "Six terraces",
    "family": "stepping",
    "lossy": true,
    "note": "Six terraces",
    "flags": "--grid 12x8 --sigma 4 --levels 6",
    "file": "../models/design_levels_6.stl",
    "thumb": "../plots/design_levels_6_print.png",
    "tris": 464,
    "size": [
      180.0,
      116.6,
      12.1
    ],
    "vol": 103.3,
    "grams": 128
  },
  {
    "id": "levels_3",
    "title": "Three plateaus",
    "family": "stepping",
    "lossy": true,
    "note": "Three plateaus: a coarse summary of the same numbers",
    "flags": "--grid 12x8 --sigma 4 --levels 3",
    "file": "../models/design_levels_3.stl",
    "thumb": "../plots/design_levels_3_print.png",
    "tris": 464,
    "size": [
      180.0,
      116.6,
      11.5
    ],
    "vol": 97.0,
    "grams": 120
  },
  {
    "id": "levels_6_facets_24x16",
    "title": "Terraces on fine facets",
    "family": "stepping",
    "lossy": true,
    "note": "Six terraces on fine facets, where steps read as contours",
    "flags": "--grid 24x16 --sigma 3 --levels 6",
    "file": "../models/design_levels_6_facets_24x16.stl",
    "thumb": "../plots/design_levels_6_facets_24x16_print.png",
    "tris": 1696,
    "size": [
      180.0,
      116.6,
      12.7
    ],
    "vol": 90.0,
    "grams": 112
  },
  {
    "id": "deep_22mm",
    "title": "Deep relief",
    "family": "depth",
    "lossy": false,
    "note": "Same shape, pushed to 22 mm",
    "flags": "--grid 12x8 --sigma 4 --height-mm 22",
    "file": "../models/design_deep_22mm.stl",
    "thumb": "../plots/design_deep_22mm_print.png",
    "tris": 464,
    "size": [
      180.0,
      116.6,
      20.6
    ],
    "vol": 149.5,
    "grams": 185
  },
  {
    "id": "carved",
    "title": "Carved",
    "family": "polarity",
    "lossy": false,
    "note": "The same numbers as valleys in a solid block",
    "flags": "--grid 12x8 --sigma 4 --invert",
    "file": "../models/design_carved.stl",
    "thumb": "../plots/design_carved_print.png",
    "tris": 464,
    "size": [
      180.0,
      116.6,
      14.5
    ],
    "vol": 251.3,
    "grams": 312
  }
];
