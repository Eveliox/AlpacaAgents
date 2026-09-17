# Agent artwork

Original PNGs supplied by the workspace owner are preserved in `originals/`.
No third-party license or redistribution rights are inferred from the upload.

| Agent | Original | Display character | Packaged avatar |
|---|---|---|---|
| Houston | `RETRO  CHARACTERS - 09.png` | Wallet | `src/alpaca_agents/assets/houston.png` |
| Star | `Group (2).png` | Spark/bomb | `src/alpaca_agents/assets/star.png` |
| Moon | `Group.png` | Ghost | `src/alpaca_agents/assets/moon.png` |
| Astra | `Group (1).png` | Vinyl record | `src/alpaca_agents/assets/astra.png` |

Display copies are optimized RGBA PNGs, proportionally resized to fit 320 × 320
using Pillow's LANCZOS filter. Pillow is only an asset-preparation tool; it is
not a runtime dependency. The dashboard embeds these assets as data URLs, so
moving the generated HTML file does not break its images.
