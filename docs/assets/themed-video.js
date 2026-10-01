// Match each recording to the docs palette, which a reader can set apart from the system color scheme.
function matchPalette() {
  const dark = document.body.getAttribute("data-md-color-scheme") === "slate"
  const systemDark = matchMedia("(prefers-color-scheme: dark)").matches
  // Films are light and dark videos that themed-video.css shows by palette; clips are one video with two sources.
  for (const video of document.querySelectorAll("video:not(.only-light):not(.only-dark)")) {
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

// A palette switch or a content tab can hide a playing recording; pause it so its narration stops with it.
function pauseHidden() {
  for (const video of document.querySelectorAll("video")) {
    if (!video.checkVisibility()) {
      video.pause()
    }
  }
}

document$.subscribe(matchPalette)
new MutationObserver(() => {
  matchPalette()
  pauseHidden()
}).observe(document.body, { attributeFilter: ["data-md-color-scheme"] })
// Content tabs are radio inputs, so switching one fires a change event.
document.addEventListener("change", pauseHidden)
