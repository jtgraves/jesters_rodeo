# Image uploads

## The constraint

Every uploaded image travels **browser → API Gateway (HTTP API) → Lambda → app
code → S3**. Three fixed ceilings sit on that path, none raisable by config:

| Limit | Value | Effect when exceeded |
| --- | --- | --- |
| API Gateway HTTP API request body | 10 MB | 413 from API Gateway; Lambda never runs |
| Lambda synchronous invocation payload | 6 MB | 413/502 before app code runs |
| Base64 inflation of the body inside the Lambda event | ~+33% | shrinks the real limit to **~4.5 MB for the whole multipart submit combined** |

A modern phone photo is 3–12 MB, so a single one can exceed the wall on its
own — and the person uploading from a phone has no way to change the file size.

## Current approach — downscale in the browser

`app/templates/admin/_image_size_guard.html` is a dependency-free `<script>`
included by every image-upload form (event create/edit, charity, beneficiaries,
clown profile). On submit it:

1. Draws each raster image into a `<canvas>` at max 1600 px on the long edge and
   re-encodes it (JPEG q≈0.82; PNGs stay PNG so transparent logos keep their
   transparency), then swaps the smaller file back into the `<input>`. A 10 MB
   photo becomes a few hundred KB.
2. Re-checks sizes as a **backstop**: > 2 MB for any one image after resizing,
   or > 4 MB combined, and it stops with a message instead of letting the
   request fail as an opaque 413. This is now rare — a huge PNG screenshot, or
   a desktop `.heic` the canvas can't decode.
3. Submits the form (`requestSubmit`, so native validation still runs).

The server still enforces `MAX_IMAGE_BYTES` (2 MB/image, in
`app/routes/admin.py`) as a second line of defence for anything that bypasses
the browser.

### Known gaps

- **Formats the canvas can't decode** (`image/heic`, `image/gif`, SVG) pass
  through unresized and hit the 2 MB backstop. iOS hands web inputs a JPEG even
  for HEIC captures, so this mostly bites a desktop user dragging in a raw
  `.heic` — the message tells them to save it as JPEG first.
- **Multi-image forms** (the charity page: logo + 3 banners + 3 carousel
  images) can still total more than 4.5 MB *after* resizing if every slot holds
  a large image. Resizing makes this unlikely, not impossible.

## Future option — presigned direct-to-S3 uploads

If per-form image counts ever make even resized uploads too big to batch, move
the upload routes off the API Gateway → Lambda path entirely:

1. Add a small endpoint (e.g. `POST /admin/uploads/sign`) that returns a
   short-lived **presigned S3 POST** (with a `content-length-range` condition
   for a hard cap).
2. The browser uploads the file **directly to S3** — S3's per-object limit is
   5 GB, and API Gateway / Lambda never see the bytes.
3. The form submits only the resulting S3 key.

Cost of doing it: the signing route, **CORS** config on the `event-images`
bucket, two-step client JS (sign → PUT → submit), and a lifecycle rule (or
nightly sweep) on an `uploads/` prefix to clear objects left by abandoned
forms. Keep the browser-side downscale from the current approach even then — it
still saves the phone user a slow multi-MB upload over a cell connection.
