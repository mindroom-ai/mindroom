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
      // A new source stops playback, so a clip that was playing on screen starts again in the other theme.
      const playing = !video.paused
      video.src = wanted
      if (playing) {
        video.play().catch(() => {})
      }
    }
  }
}

// Each recording plays while at least half of it is on screen, and pauses once it scrolls away or a palette switch
// or content tab hides it, since hidden elements stop intersecting.
const onScreen = new IntersectionObserver(
  (entries) => {
    for (const { target, isIntersecting } of entries) {
      // play() rejects when a quick scroll pauses the video before it starts; there is nothing to recover.
      isIntersecting ? target.play().catch(() => {}) : target.pause()
    }
  },
  { threshold: 0.5 },
)

document$.subscribe(() => {
  onScreen.disconnect()
  for (const video of document.querySelectorAll("video")) {
    // Browsers only start a video on their own while it is muted; its controls can unmute it.
    video.muted = true
    video.loop = true
    onScreen.observe(video)
  }
  matchPalette()
})
new MutationObserver(matchPalette).observe(document.body, { attributeFilter: ["data-md-color-scheme"] })
