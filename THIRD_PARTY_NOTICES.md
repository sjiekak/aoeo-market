# Third-Party Notices

`aoeo_market`'s own source code is licensed under the MIT License (see
[LICENSE](LICENSE)). The repository **also contains data derived from, and in
some places copies of, content that is not the author's**. That content is
**not covered by the MIT License**, and this project cannot grant any rights in
it. This file records what that content is, where its terms come from, and what
has to travel with it when the project (or a modified version of it) is
redistributed or hosted.

If you redistribute this project, keep this file, keep the notices below
verbatim, and keep the link to the Xbox Game Content Usage Rules.

---

## 1. Microsoft — Age of Empires Online game content

The item names, descriptions, gear types, icon artwork and the Dismantler guide
are content from **Age of Empires Online**, © Microsoft Corporation. This
project makes that content available in reliance on Microsoft's **Game Content
Usage Rules** (Last Updated: January 2015):

- <https://www.xbox.com/en-us/developers/rules>

**Permission to use this content comes from Microsoft under those rules, not
from this project.** The MIT License in this repository grants nothing in it.
The rules' licence is personal and non-transferable, so everyone who uses the
content receives it from Microsoft directly, on Microsoft's terms.

Some content listed below may fall outside what the rules permit; see §5.

### Required notice

The rules require this notice, together with a link to the rules, wherever the
project is shared, so that anyone who sees the project can easily find both:

> Age of Empires Online © Microsoft Corporation. AoEO Market was created under
> Microsoft's "Game Content Usage Rules" using assets from Age of Empires
> Online, and it is not endorsed by or affiliated with Microsoft.

For this repository, the notice lives in this file, which is linked from
`README.md`. For the dashboard, whose visitors never see the repository, the
full notice and a link to <https://www.xbox.com/en-us/developers/rules> appear in
the page footer.

### What carries Microsoft game content

| Path | Committed? | What it is |
| --- | --- | --- |
| `aoeo_market/data/catalog.json` | Yes | Item display names, descriptions, gear types, civilizations, ages, icon ids and crafting-recipe structure (4,164 entries), from the game's item data by way of `celeste-search` (§2). |
| `images/gear_dismantler/*.png` | No (gitignored) | Rendered pages of the in-game Dismantler Output Guide texture, generated locally from the user's own game installation. See §5. |
| `aoeo_market/web/static/sprites.json` | Yes | The icon → sprite-position index. The index is generated from `celeste-search` (§2); the icons it points at are game artwork. |
| `aoeo_market/web/static/sprites/*.webp` | No (gitignored) | The item icon sprite sheets, regenerated locally by `scripts/build_sprites.py` from a `celeste-search` checkout. They are Microsoft game content; if you redistribute them, the rules apply to them. |

### Summary of the terms

This summary is for convenience; the rules themselves govern. They grant a
**personal, non-exclusive, non-sublicenseable, non-transferable, revocable,
limited license** to use and display the game content and to create derivative
works, for personal, noncommercial use except where the rules specifically say
otherwise. In particular:

- **Noncommercial.** You may not sell the project or earn compensation from it,
  including through advertising in it, a paywall or subscription, or by placing
  it on a page you use to sell other goods or services. A free app must stay
  free and must not carry advertising. Pages with optional donation requests
  are permitted, and videos of the project may take part in YouTube or Twitch
  ad-revenue programmes.
- **No commercial promotion.** You may not use the game content to promote a
  commercial venture, including a commercial website, without a commercial
  licence from Microsoft.
- **No exclusive distribution.** You may not agree to let anyone distribute the
  project exclusively, even if they do not pay you.
- **No reverse engineering.** You may not reverse engineer the games to access
  their assets, or otherwise do things the games do not normally permit, in
  order to create your Item. See §5.
- **No endorsement.** Do not use Microsoft's or the game's logos, and do not use
  the game's name in a way that suggests Microsoft produced, authorised or
  endorsed the project. Referential use of the title is fine.
- **No infringement, malware, adware or spyware, and nothing that leads to spam
  or phishing.**
- **Downstream users are bound too.** Anyone who uses or builds on this project
  must follow the same rules, and may not earn money from their work on it
  except as the rules allow.
- **The licence is revocable.** Microsoft may withdraw it at any time and for
  any reason. If that happens, stop distributing the game content.
- **Licence back to Microsoft.** Distributing an Item grants Microsoft, its
  partners and its users a royalty-free, irrevocable, worldwide licence to use,
  modify and distribute that Item and its derivatives. The MIT License already
  permits as much for this project's source code, but the grant applies
  regardless.

---

## 2. Project Celeste — `celeste-search` (ISC License)

`aoeo_market/data/catalog.json` and `aoeo_market/web/static/sprites.json` are
generated by `scripts/build_catalog.py` and `scripts/build_sprites.py` from the
curated item database and stylesheets of the Project Celeste search app
(`ProjectCeleste/celeste-search`, served at <https://search.projectceleste.com>),
at commit `ba70af04397326e94e5925c9c7c9b442850ceb41`. That project is licensed under the ISC License,
reproduced verbatim from its `LICENSE` file at that commit:

```text
ISC License

Copyright (c) 2018-2019 Abraham Schilling

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

Note the split: the **database structure, ids and curation** in those generated
files come from `celeste-search` and are covered by the ISC License above. The
**item names, descriptions and artwork** they carry are Microsoft game content
(§1), which Project Celeste does not own and cannot license.

---

## 3. Project Celeste — launcher and game-file tooling (GPL-3.0, not redistributed)

The `ProjectCeleste/Celeste.Launcher` and
`ProjectCeleste/ProjectCeleste.GameFiles.GameScanner` trees, consulted as
protocol reference material, are licensed under **GNU GPL-3.0**. They are **not
part of this repository** (`ProjectCeleste*` is gitignored) and are not
redistributed. No code from those trees is included in this project;
`aoeo_market`'s protocol implementation was written independently.

If code from those trees is ever copied or translated into `aoeo_market`, the
combined work would have to be distributed under GPL-3.0, and the project could
no longer be offered under the MIT License alone. Any such change must be
reviewed for licensing before it is merged.

---

## 4. Runtime and development dependencies (installed, not vendored)

These are resolved from PyPI per `pyproject.toml` / `uv.lock` and are **not
included in this repository**. Each keeps its own licence:

| Package | Licence |
| --- | --- |
| `duckdb` | MIT |
| `lxml` | BSD-3-Clause |
| `scapy` | GPL-2.0-only |
| `pytest` (dev) | MIT |
| `ruff` (dev) | MIT |
| `openapi-schema-validator` (dev) | BSD-3-Clause |
| `openapi-spec-validator` (dev) | Apache-2.0 |

`scapy` deserves a specific note: it is **GPL-2.0-only** and is used only by
`aoeo_market/pcap_source.py` to read packet captures. This repository does not
include scapy, and distributing the repository's source does not change the
licence of that source. If you **distribute a bundle that contains scapy** (for
example a container image or a frozen executable), that distribution must
satisfy GPL-2.0 for scapy: ship its licence text and make its corresponding
source available.

Any bundle will also contain the transitive dependencies of the packages above,
each under its own licence. Generate a complete licence list at build time (for
example with `pip-licenses`) and ship it with the bundle.

---

## 5. Known limits of these permissions

- **Access to Project Celeste's servers.** This project connects to the Project
  Celeste game service by replaying a login exchange observed in packet
  captures, including the `xlive.dll` CRC-32 and a per-install device
  fingerprint. Microsoft's rules do not govern Project Celeste's servers;
  automated access to them is subject to Project Celeste's own terms, and
  nothing in this file grants permission for it. No packet captures,
  credentials, session tokens or device fingerprints are included in this
  repository. Running the tool against Project Celeste's servers is at your own
  risk.
- **Revocation.** If Microsoft revokes the licence or asks for game content to
  be removed, distribution of the content listed in §1 will stop, and anyone
  redistributing this project is expected to do the same.

---

## 6. If you redistribute or host this project

- **Keep it noncommercial.** No ads, paywall, subscription, premium tier or
  charge for the app or the hosted dashboard. Optional donation requests are
  allowed.
- **Keep the notices reachable.** Keep this file, keep it linked from
  `README.md`, and keep the full notice from §1, with the link to the rules, in
  the dashboard footer.
- **Keep it independent of commercial ventures.** Do not host it on a
  commercial website or bundle it with a commercial product or service.
- **Keep dependency licences with bundles.** If you distribute a bundle that
  includes dependencies, follow §4.

---

## 7. Independence and trademarks

This project is not endorsed by or affiliated with Microsoft or with Project
Celeste.

Age of Empires, Age of Empires Online, Microsoft and Xbox are trademarks of the
Microsoft group of companies. They are used here only referentially, to
identify the game this fan tool works with. No Microsoft or Age of Empires logos
are included, and their use does not imply any sponsorship or endorsement.
