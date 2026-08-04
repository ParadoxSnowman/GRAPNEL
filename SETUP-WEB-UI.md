# Setting this up entirely in the GitHub web UI

GitHub's drag-and-drop uploader **silently discards any folder whose name starts
with a dot**. No warning, no error. That is why `.github/` never arrived in your
repo, and why there was no Action to run. Same fate for `.gitignore` and
`docs/.nojekyll`.

You cannot *upload* a dot-folder through the browser. You **can create one**, by
typing a path with slashes into the filename box.

---

## 1. Add the workflow (this is the missing piece)

1. On the repo page: **Add file → Create new file**
2. In the filename box type exactly, including the slashes:

   ```
   .github/workflows/monitor.yml
   ```

   As you type each `/`, GitHub turns the segment into a folder. This is the
   only way to make a dot-folder from the browser.
3. Open `WORKFLOW-monitor.yml` in the repo root, click **Raw**, select all, copy.
4. Paste it into the editor.
5. **Commit changes** to `main`.

The Actions tab now lists **monitor**, with a *Run workflow* button.

`WORKFLOW-monitor.yml` at the root is only a visible copy so you can read it in
the browser. Delete it once the real one exists, or leave it; it does nothing.

## 2. Add `.nojekyll`

Same method. **Add file → Create new file**, filename:

```
docs/.nojekyll
```

Leave the body empty and commit. Without it, Pages runs your site through
Jekyll, which silently skips files and folders beginning with an underscore.
Nothing here starts with one today, but it costs nothing to stop the surprise.

## 3. Refresh the map data

Your site currently serves an older build. Replace it:

1. Go to the **`docs`** folder in the repo
2. **Add file → Upload files**
3. Drag in the **contents** of `docs/` from the new zip — `index.html`, the
   `assets` folder, and the `data` folder
4. Commit

`data/` alone is ~2.3 MB across seven files, all under the 25 MB per-file web
limit. If the browser struggles with the folder drag, upload `data/` and
`assets/` as two separate commits.

## 4. Check it

- **Actions tab** → monitor → *Run workflow* → Run
- **Settings → Pages** should already say *Deploy from a branch*, `main`, `/docs`
- The map should show ~503 cables and the tagline
  "503 submarine cables · 1,335 landing points · global AIS"

If the tagline still reads the old one, the `docs/` upload did not land.

## Why the Action is optional

The map is a finished static site. It publishes from `docs/` with no workflow at
all. The workflow only refreshes AIS data on a schedule — and it needs an
`AISSTREAM_API_KEY` secret (free key at https://aisstream.io) to have any AIS to
fetch:

**Settings → Secrets and variables → Actions → New repository secret**
Name `AISSTREAM_API_KEY`, value your key.

Without that secret the workflow runs, finds no AIS source, keeps the existing
payload and exits non-zero. The cables still show.
