# AHP Week in Review

The site at `briefs.rheumacademy.com`. Each week's brief gets a preview page
with all ten headlines and a few items in full. The complete PDF goes to
email subscribers.

## Publishing an issue

1. Add the finished PDF to `briefs/`, named for its date: `briefs/2026-11-27.pdf`.
2. That is all. A GitHub Action reads the PDF and builds the preview page,
   the front page and the feed. It takes about a minute.

The page appears at `briefs.rheumacademy.com/2026-11-27/`.

If the PDF cannot be read as a brief, the Action fails and nothing is
published. GitHub emails the failure, and the reason is in the Actions tab.

## Choosing which items are open

The first three items are shown in full by default. To change that, open
`issues/2026-11-27.json`, edit the line

    "open_items": [1, 2, 3]

and save. The page is rebuilt.

## Correcting an issue

Replace the PDF with the corrected one, same file name. The page is rebuilt
from it and your `open_items` choice is kept.

To change the web text without touching the PDF, edit the issue's `.json`
file and set `"locked": true`, so it is not read from the PDF again.

## What is public

Nothing on the site links to the PDFs. They stay at
`briefs.rheumacademy.com/briefs/DATE.pdf` so the link in the subscriber email
works. That makes them unlisted, not locked: anyone with the address can open
one, and this repository is public.

## Files

```
briefs/DATE.pdf      the finished brief (you add this)
briefs/DATE.jpg      page 1 as an image, made automatically if missing
issues/DATE.json     the issue as data, made from the PDF
DATE/index.html      the preview page, built
index.html           the front page, built
feed.xml             RSS feed, built
build.py             the builder
```

`index.html`, `feed.xml` and the `DATE/` folders are rebuilt every time, so
edits made directly to them are overwritten. Change the look in `build.py`.

To build on your own computer: `pip install pypdf` then `python build.py`.
