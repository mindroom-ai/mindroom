// Match each recording to the docs palette, which a reader can set apart from the system color scheme.
function matchPalette() {
  const dark = document.body.getAttribute("data-md-color-scheme") === "slate"
  const systemDark = matchMedia("(prefers-color-scheme: dark)").matches
  for (const video of document.querySelectorAll("video")) {
    if (video.matches(".only-light, .only-dark")) {
      // themed-video.css hides the film recording for the other palette; pause it so its narration stops.
      if (!video.checkVisibility()) {
        video.pause()
      }
      continue
    }
    // A clip's dark source carries media="(prefers-color-scheme: dark)", so without this the system scheme picks.
    const sources = [...video.querySelectorAll("source")]
    if (sources.length !== 2 || (dark === systemDark && !video.hasAttribute("src"))) {
      continue
    }
    const wanted = sources.find((source) => source.hasAttribute("media") === dark).getAttribute("src")
    if (video.getAttribute("src") !== wanted) {
      video.src = wanted
    }
  }
}

document$.subscribe(matchPalette)
new MutationObserver(matchPalette).observe(document.body, { attributeFilter: ["data-md-color-scheme"] })
