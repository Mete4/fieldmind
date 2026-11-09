# Sample images (optional, Phase 5: vision)

Drop drone or satellite photos here (`.jpg`/`.png`); they are sent to
`POST /ingest/image` and described by the local vision model (qwen2.5vl:7b).
The folder may stay empty; the dashboard simply hides the image demo button.

Good public sources (check each license before redistribution):

- **xBD / xView2** (Maxar satellite imagery with building-damage labels,
  pre/post disaster, includes several California wildfires):
  <https://xview2.org/dataset>. License: CC BY-NC-SA 4.0 (non-commercial).
- **FloodNet** (UAV images after Hurricane Harvey, flooded/non-flooded
  buildings and roads): <https://github.com/BinaLab/FloodNet-Supervised_v1.0>.
  Research use; see the repository for the license terms.
- **Maxar Open Data Program** (event imagery released for humanitarian use,
  including the November 2018 Camp Fire in Paradise, CA):
  <https://www.maxar.com/open-data>. License: CC BY-NC 4.0.
- **NASA FIRMS** fire detections are already loaded as a map layer
  (`data/hazards/firms.geojson`) when a free MAP_KEY is set.

Name files descriptively (e.g. `camp_fire_pentz_rd.jpg`); the optional
`location_text` form field on upload is geocoded against the real gazetteer.
