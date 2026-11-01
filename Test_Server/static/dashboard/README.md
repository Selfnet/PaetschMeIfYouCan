# Dashboard Assets

The game serves these assets locally and does not depend on their source sites
being reachable. Selfnet branding assets are copied unchanged.

- `selfnet.svg`: the full wordmark from [Selfnet's blog repository](https://git.selfnet.de/www/blog),
  `src/assets/logos/selfnet.svg` at `22ca293`. Preserve the original proportions,
  alignment and artwork color; this is not the cropped SIAM flag. The bundled
  [blog license](../../../docs/licenses/Selfnet-blog-MIT.txt) retains the source
  repository's copyright and redistribution notice, not game behavior.
- `bkgrnd.jpg`: SIAM's concrete background from `support/siam`,
  `src/siam/static/img/bkgrnd.jpg`.
- `Marvel-Regular.ttf` and `Marvel-Bold.ttf`: SIAM's display fonts from
  `src/siam/static/fonts/Marvel/`. See the bundled
  [Marvel Open Font License](../../../docs/licenses/Marvel-OFL.txt) for font
  copyright and redistribution terms; it does not license the Selfnet artwork.
- `ComicNeue-Bold.ttf`: the approved open-source maintenance-screen font from
  [Google Fonts](https://github.com/google/fonts/tree/main/ofl/comicneue), copied
  unchanged. The bundled [Comic Neue OFL](../../../docs/licenses/ComicNeue-OFL.txt)
  permits font redistribution; it does not cover the game or Selfnet artwork.

SIAM's `src/siam/static/css/custom.css` is the source for the gray/orange visual
direction and Helvetica body text, not the game's layout or interaction rules.
The [game README](../../../README.md) owns the keyboard and scoring contracts.
