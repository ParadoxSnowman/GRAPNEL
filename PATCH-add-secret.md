# One edit fixes the zero-vessel run

## The problem

A GitHub repository secret is **not** an ambient environment variable in the
runner. Adding it under Settings does nothing on its own — each step that needs
it must map it explicitly. Without that mapping the AIS source received an empty
key, could not connect, and returned nothing. Preflight stayed green because it
was not checking for the key at all.

## The edit

In your repo, open **`.github/workflows/monitor.yml`**, click the pencil, and
find this block (around line 80):

```yaml
      - name: Run pipeline
        id: pipeline
        continue-on-error: true
        timeout-minutes: 15
        run: |
```

Insert an `env:` block so it reads:

```yaml
      - name: Run pipeline
        id: pipeline
        continue-on-error: true
        timeout-minutes: 15
        env:
          AISSTREAM_API_KEY: ${{ secrets.AISSTREAM_API_KEY }}
          AISSTREAM_SECONDS: ${{ vars.AISSTREAM_SECONDS || '180' }}
        run: |
```

Do the same for the preflight step so the diagnostic can see the key too:

```yaml
      - name: Preflight
        continue-on-error: true
        env:
          AISSTREAM_API_KEY: ${{ secrets.AISSTREAM_API_KEY }}
        run: python scripts/doctor.py --ci
```

Indentation matters: `env:` sits at the same level as `run:`, six spaces in.

Commit, then **Actions → monitor → Run workflow**.

Easier alternative: replace the whole file with the contents of
`WORKFLOW-monitor.yml` in the repo root, which already has both blocks.

## What you should see next run

```
PASS  AISSTREAM_API_KEY present — 40 characters, ends xxxx
PASS  websockets 12.x
```

then, after the ~3 minute collection window:

```
aisstream: 40000+ positions, 8000+ vessels (5.2 fixes each)
```

and vessels on the map. Detections need tracks, so the first run gives you the
live vessel layer and the in-corridor watchlist; behavioural detections build up
over subsequent runs as the archive fills.

## Also check requirements.txt

The aisstream source needs the `websockets` package. Open `requirements.txt` in
your repo and confirm the last line is:

```
websockets>=12.0
```

If it is missing, add it and commit. Preflight now fails loudly if it is absent
rather than returning an empty result.

## If it still collects nothing

The log will now say which of these it is:

- **"connection closed after Ns with no data"** → the key was rejected. Check it
  at https://aisstream.io.
- **"AISSTREAM_API_KEY is empty"** → the `env:` mapping did not take. Check
  indentation.
- **"aisstream server error: ..."** → the server said why; the message is
  printed verbatim.
