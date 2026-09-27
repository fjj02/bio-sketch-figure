---
name: bio-sketch-figure
description: "Use when the user wants academic or scientific figures produced — paper illustrations, mechanistic diagrams, pathway schematics, graphical abstracts, or journal-cover-style artwork. Accepts a research topic plus optionally 1-5 reference images. Triggers: 科研配图, 论文配图, 机制图, Graphical Abstract, BioSketch, 2.5D 科研风格, 2.5D高级, 平面矢量风格, scientific figure, research illustration, graphical abstract."
---

# bio-sketch-figure — Scientific Figure Generation

Give it a topic: **topic (+ reference images) → content outline → page-by-page figures**, all handled by the BioSketch service.

## Five Iron Rules

1. **Everything must go through this skill** — image understanding, outline planning, and figure
   rendering are all done by `scripts/bio_sketch_figure.py`. **Never** substitute your own vision
   or your built-in image-generation tools (e.g. `analyze_image` / `generate_image`).
   All you need is the ability to run `python`.
2. **Always ask about style first** — if `--style` is missing, the command exits with an error and
   prints the style list. You **must** run `styles`, show the list to the user, and get an explicit
   answer before continuing. **Never pick a default style on your own.**
3. **Never expose credentials** — access credentials belong only in the user-level config or
   environment variables. Never write them into the skill directory, never print them in logs or
   replies, never commit them.
4. **Never spend image quota without explicit user consent** — rendering costs money.
   Before running any command that generates images (`render` / `all`), you **must** tell the user:
   how many pages (= how many images) will be generated, which style, which reference images, and
   then **wait for an explicit "yes"**. Only pass `--confirm` after the user has agreed.
   Without `--confirm`, `render` refuses to run (exit code 1) and `all` stops right after the
   outline (text-only), printing the plan. **Do not pass `--confirm` on your own initiative.**
5. **Never redraw an existing figure without explicit user consent** — by default `render` / `all`
   **skip** pages whose `<index>.png` already exists (only missing/failed pages are filled, no extra
   billing). Re-generating an existing page requires `--overwrite`, which is itself a **billed**
   operation: you **must** show the plan (including the "重绘/覆盖" line), tell the user exactly how
   many already-generated pages will be overwritten, and **wait for an explicit "yes"** before adding
   `--confirm --overwrite`. Never pass `--overwrite` on your own initiative.

## Requirements

- Ability to run `python` (dependencies are already available)
- Credentials must be set up once (see Step 0)
- On Windows, if the terminal reports `uv_spawn 'cmd.exe'`, start the shell with
  `C:\Windows\System32` as the working directory, then `cd /d` into the skill directory

## Step 0: Set up credentials

```bash
python <skill-dir>/scripts/bio_sketch_figure.py check              # show status (credentials masked, no model call)
python <skill-dir>/scripts/bio_sketch_figure.py init --text-key <K> --image-key <K>
```

If the user pastes credentials directly, run `init` to store them in the user-level config.
**Never repeat the credentials back in your reply.**

## Step 1: Style gate (mandatory)

```bash
python <skill-dir>/scripts/bio_sketch_figure.py styles
```

Pass the list to the user verbatim and wait for an answer:

| id | Name | Notes |
|---|---|---|
| `2.5d` | 2.5D 科研风格 | Semi-3D, suited to journal covers / graphical abstracts |
| `flat` | 平面矢量风格 | Flat vector, suited to workflow and mechanism diagrams |
| `2.5d-advanced` | 2.5D高级 | Narrative journal-cover illustration; ships with its own style reference image that is applied automatically. Default aspect ratio 4:5 |
| `xinfenge` | 2.5D 柔彩机制图 | Soft semi-3D biomedical mechanism illustration; ships with its own style reference image. Default aspect ratio 3:2 |
| `liuchengtu` | 科研流程图 | Academic technical roadmap / architecture flowchart; ships with its own style reference image. Default aspect ratio 3:4 |
| `biorender` | BioRender 科研插画 | Flat micro-3D biomedical illustration (low-saturation Morandi palette, white background, tonal outlines); ships with its own style reference image. Default aspect ratio 4:3 |
| `none` | 无风格参考 | No visual style constraint |

Do not proceed to rendering until the user has made an explicit choice.

## Step 2: Generate the outline

```bash
python <skill-dir>/scripts/bio_sketch_figure.py outline "<research topic>" [--ref image1 image2 ...]
```

- If reference images are supplied, the skill **recognises their content automatically** and uses it
  when planning the outline (the log prints `[识图] 已发送 N 张参考图`)
- Output: `<out_dir>/<task_id>/outline.txt` and `pages.json`
  (`[{index,type,content}]`, where type is `cover` / `content` / `summary`)
- Read the per-page summary back to the user and get confirmation before rendering

**Text quota**

- The text model is called **exactly once per `outline`** (reference-image recognition is merged
  into that same call) and is **never** called by `check` or `render`.
- Automatic retries are disabled for the text model, so a failure costs at most one request.
  If it fails, rerun `outline` manually — that is a new (single) billed call.

## Step 3: Render the figures (costs image quota — confirm first)

```bash
python <skill-dir>/scripts/bio_sketch_figure.py render \
  --pages <out_dir>/<task_id>/pages.json \
  --style <confirmed style id> \
  [--ref image1 ...] [--concurrency 3] [--aspect-ratio 4:5] [--topic "<research topic>"] \
  --confirm
```

Or run both steps at once (`--style` is still required, and you must still ask first):

```bash
python <skill-dir>/scripts/bio_sketch_figure.py all "<research topic>" --style <id> [--ref ...] [--concurrency 3] --confirm
```

**Quota gate (Iron Rule 4)**

- `render` / `all` **require `--confirm`**. Without it, `render` exits with code 1 and only prints the
  plan (page count = number of images, style, reference images, aspect ratio). `all` without
  `--confirm` runs the outline only (text quota) and then prints the plan.
- The plan shows exactly how many images will be generated. Show it to the user and **wait for an
  explicit yes** before adding `--confirm`.

**Rendering rules**

- **Every page is generated independently** — there is no cover pass and no page-to-page reference
  chaining. Cross-page consistency comes from the full outline, the original topic, a shared
  **style lock** (a fixed consistency block plus each style's `palette`), and the element library.
- **Reference-image precedence per page**: user images → the style's reference image → element
  assets matched from the library, filling up to `defaults.max_ref_images` (default 5).
  When the selected style ships a reference image, it is **guaranteed a slot**: user images are
  capped at `max_ref_images - 1`, so the style image can never be pushed out.
  - **Style basis**: if the user supplied reference images, they are the **priority** style/composition
    basis and the style's reference image is labelled as a **secondary** style reference; if no user
    images were supplied, the style's reference image is the **sole** style basis.
  - Image size limit: the style image uses the style's `reference_max_kb` when set (a larger budget
    preserves brush/gradient detail; `2.5d-advanced` / `xinfenge` / `liuchengtu` ship 400KB), all
    other references use the global `image.reference_max_kb` (default 200KB).
  Element assets are only added when a keyword actually matches the page content
  (at most `defaults.max_elements_per_page`, default 2). The log prints
  `[出图] 第 N 页 参考图: <decision>` for every page.
- Element assets are labelled `【图N】元素素材「名称」— 请直接复用其造型、结构与配色`.
- `--concurrency` defaults to 1 (sequential); use 3 to speed things up.
- **Network resilience**: a stream interruption during submission is auto-handled — once the task id
  is known the run switches to polling recovery (the task is never re-submitted, so it is not billed
  twice); if the id was not yet received it retries the submit (up to `SUBMIT_MAX_RETRIES`). Fetching
  the final image also retries. A transient proxy/network blip no longer kills a page.
- **Resume**: `render` / `all` **skip pages whose `<index>.png` already exists**. After a partial
  failure, just re-run the same command to fill only the missing pages (no re-generation, no extra
  billing). `--overwrite` forces a redraw of existing pages — it is billed and **requires explicit
  user consent first** (Iron Rule 5); the plan gate prints the redraw line.
- **Proxy / direct**: this skill **connects directly by default** and **ignores** the
  `HTTP_PROXY` / `HTTPS_PROXY` environment variables (`Session.trust_env = False`) — the relay is a
  domestic node, and a global/broken proxy usually makes it fail. To use a proxy, set
  `text.proxy` / `image.proxy` to its URL explicitly (`none` / empty = direct). If an explicitly
  configured proxy cannot connect, the run logs it and **falls back to direct** automatically.
  Do **not** wrap the skill in `proxychains4` — an LD_PRELOAD proxy cannot be bypassed by the script.

## Element library (`sucai/`)

`styles/*.yaml` carry a `palette` list used by the style lock and may set `reference_max_kb`
(the style image's size budget in KB; falls back to the global `image.reference_max_kb` when
absent); `sucai/index.yaml` indexes reusable
scientific elements (organelles, organs, labware, model organisms, materials, …). Each entry has
`id / name / keywords / description / file / transparent`. A page only receives the assets whose
`keywords` appear in that page's outline text. Add new assets by dropping the image into `sucai/`
and adding an entry to `sucai/index.yaml`; `check` reports files that exist but are not indexed.

## Output

```
<out_dir>/<task_id>/
├── outline.txt        # raw outline text
├── pages.json         # parsed pages
├── 0.png 1.png ...    # one figure per page
├── thumb_0.jpg ...    # thumbnails (≤50KB)
├── run.json           # style / reference images / aspect ratio for this run
└── failed.json        # failed pages, if any
```

## Troubleshooting quick reference

| Symptom | Action |
|---|---|
| `❌ 缺少 --style` | Expected gate. Go ask the user. **Do not add a default.** |
| `[!] 出图是计费操作…` (exit 1) | Expected quota gate. Show the plan to the user; add `--confirm` **only after they agree**. |
| `❌ 未配置规划/出图凭证` | Run Step 0 `init`, or set the environment variables |
| Credentials rejected | Double-check the credentials (watch for leading/trailing spaces) |
| Too many requests | Already retried automatically; if it still fails, wait and retry |
| Service cannot recognise reference images | Remove the reference images and rerun |
| Recognition failed / image rejected | Relay the message and let the user decide. **Never switch to your own vision.** |
| A page got no element asset | Expected: no keyword matched, or reference slots were full (`名额已满`). Not an error. |
| A single page failed | Check `failed.json`, then rerun `render` with the same `--pages` |
| Generation timed out | Retry later; the run is recorded in `run.json` |

More detail in `references/troubleshooting.md`.

## Related files

- `scripts/bio_sketch_figure.py` — the only engine (`init / check / styles / outline / render / all`)
- `tests/test_bio_sketch_figure.py` — test suite (`python tests/test_bio_sketch_figure.py`)
- `prompts/style_lock.txt` — the cross-page consistency block used by every page
- `styles/*.yaml` — style definitions (`palette`, optional `reference_image`); `2.5d-advanced.yaml` carries a style reference image
- `assets/styles/2.5d-advanced.png` — the 2.5D高级 style reference image
- `assets/styles/biorender.jpg` — the BioRender 科研插画 style reference image
- `sucai/index.yaml` + `sucai/*.png` — reusable element-asset library
- `config.example.yaml` — config template (placeholders only)
- `references/troubleshooting.md` — FAQ
