# How `acm-badged.csv` was selected

The rule, counts and exclusions are in [`../acm-badged-selection.md`](../acm-badged-selection.md).
These scripts produced it. Run them in this folder, in order:

```bash
git clone https://github.com/sysartifacts/sysartifacts.github.io sys && git -C sys checkout 673135d5f59cd74ba7c6b7a8f94e1ab630b25d71
git clone https://github.com/secartifacts/secartifacts.github.io sec && git -C sec checkout 8283e3f372884cf0f2eab84f1a7c7119d1e54fca
python parse.py    # all results.md front matter -> all.json
python pool.py     # ACM-badge sections only -> pool.json
python assess.py   # host, link, language, compose and GPU checks -> assess.json (network; needs `gh auth`)
python elig.py     # first failing criterion per row -> res.json
python sel.py      # sort eligible rows by title, every 3rd -> sel.json
python out.py      # -> ../acm-badged.csv and ../acm-badged-selection.md
```

`assess.json` is committed as accessed on 2026-11-22. `assess.py` keeps every
row it holds except those that errored or were unreachable, which it queries
again, so a re-run reproduces the selection unless one of those rows has come
back. Delete it to re-query GitHub, GitLab and Zenodo from scratch. Repositories change and links rot, so a fresh assessment can
select differently.
