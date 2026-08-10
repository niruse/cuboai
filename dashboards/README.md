# The example dashboard — what to change before it works

These files are a **worked example, not a drop-in**. They are written against a
camera whose baby is called `mia`, and Home Assistant builds entity ids from
*your* camera's name, your area and your own renames. Copy them unchanged and
every card renders empty or errors.

Nothing here is destructive to get wrong — a mistyped entity id shows an error
card, it does not break your camera — but nothing will work until you swap the
ids over.

> **Copying to a machine that already has these files?** Your live copy holds
> *your* ids. Re-copying from the repo overwrites them with `mia` again and the
> dashboard goes blank. Edit in place, or re-apply the replacements below.

## 1. Find your own entity ids

Developer Tools → **States**, filter on `cuboai`. Or, faster, from a terminal:

```bash
ha state list | grep cuboai
```

You are looking for the four the dashboard leans on. On the example camera they
are:

| Purpose | Example id | Yours will look like |
|---|---|---|
| Presence from the DVR history | `sensor.cuboai_mia_cuboai_baby_present_mia` | `sensor.<area>_cuboai_<baby>_cuboai_baby_present_<baby>` |
| Motion | `sensor.cuboai_mia_cuboai_motion_mia` | same shape, `_motion_` |
| Noise level | `sensor.cuboai_mia_cuboai_noise_level_mia` | same shape, `_noise_level_` |
| Camera online | `sensor.cuboai_camera_state_mia` | `sensor.cuboai_camera_state_<baby>` |

The area prefix (`nursery_`, `upstairs_`, …) appears only if you have
assigned the device to an area, which is why the example cannot guess it. **Copy
the ids exactly as Home Assistant shows them.**

## 2. Replace them in both files

Both files must match — the package builds the sensors, the dashboard displays
them.

```bash
# Replace mia_baby_present with YOUR full entity id, and so on.
sed -i 's/sensor\.cuboai_mia_cuboai_baby_present_mia/sensor.YOUR_PRESENCE_ID/g' \
       dashboards/cuboai.yaml dashboards/packages/cuboai_sleep.yaml
sed -i 's/sensor\.cuboai_mia_cuboai_motion_mia/sensor.YOUR_MOTION_ID/g' \
       dashboards/cuboai.yaml
sed -i 's/sensor\.cuboai_mia_cuboai_noise_level_mia/sensor.YOUR_NOISE_ID/g' \
       dashboards/cuboai.yaml
sed -i 's/sensor\.cuboai_camera_state_mia/sensor.YOUR_CAMERA_STATE_ID/g' \
       dashboards/cuboai.yaml
```

On Windows, or if you would rather not use `sed`, open both files and use your
editor's find-and-replace on the same four strings.

### The `mia_` sensors are different — leave them alone

`sensor.mia_night_in_crib`, `sensor.mia_week_coverage` and friends are **created
by the package itself**, from the `name:` fields in `cuboai_sleep.yaml`. They are
not your camera's entities.

Either leave every `Mia` name in that file exactly as it is (the sensors will be
called `sensor.mia_night_in_crib` and the dashboard already points at them), or
rename them **in both files together**. Renaming in only one is the one mistake
here that produces a dashboard of "Entity not available" with no obvious cause.

## 3. Check before restarting

```bash
ha core check
```

Or Developer Tools → YAML → *Check configuration*. A YAML error stops Home
Assistant from starting cleanly, so it is worth the ten seconds.

## 4. Restart, then hard-refresh

A restart is required — packages and YAML dashboards are read only at startup.
Then hard-refresh the browser (Ctrl+Shift+R) or force-close the Companion app.
**A cached card is the single most common reason a change appears not to have
landed.**

## What "empty" means, and when it is not a fault

- **In crib reads 0h.** Correct until the camera has actually reported someone
  in the crib. An empty room is a reading, not a failure.
- **In crib / Sleeps stay at 0 forever?** The presence readings only exist when
  the camera's **baby presence / sleep-safety detection is turned on** (in the
  CuboAI app, or via the integration's Baby Presence switch). With it off the
  camera never reports "in crib", so the tiles and the sleep (zzz) lane
  legitimately read 0 — that is the camera's setting, not a dashboard fault.
- **There is no `Caregiver?` lane anymore.** The wellbeing bit it watched was
  put to the test by a real, known 2 a.m. visit — and produced none of the
  states upstream guessed would mark one. A visit shows up as **strong
  motion**, which the Moving lane now matches (`moving`, `strong (2)`,
  `strong (3)`).
- **Every window also reports how long the sensor said nothing.** If that figure
  is large, the figures beside it cover only part of the window — an offline
  camera and an empty room otherwise look identical.
- **The `Camera online` lane** on the timeline exists for the same reason.
- **`Not in crib` counts both "not in crib" and `0`.** What `0` means is not
  documented by the camera; it is treated as "not in the crib" because it holds
  steady while a room is empty. See the comments at the top of
  `packages/cuboai_sleep.yaml`.

## Files

| File | Goes to | Purpose |
|---|---|---|
| `cuboai.yaml` | `/config/dashboards/cuboai.yaml` | The five tabs |
| `packages/cuboai_sleep.yaml` | `/config/packages/cuboai_sleep.yaml` | The `history_stats` sensors behind them |

Full installation steps, including registering the dashboard in
`configuration.yaml`, are in the main [README](../README.md#-installing-the-full-dashboard).
