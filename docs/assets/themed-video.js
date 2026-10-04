// Match each recording to the docs palette, which a reader can set apart from the system color scheme.
// A recording that started keeps its theme until it leaves the screen, so a palette switch never restarts or hides it.
function matchPalette() {
  const dark = document.body.getAttribute("data-md-color-scheme") === "slate"
  const systemDark = matchMedia("(prefers-color-scheme: dark)").matches
  // Films are light and dark videos that themed-video.css shows by palette; clips are one video with two sources.
  for (const video of document.querySelectorAll("video:not(.only-light):not(.only-dark):not(.started)")) {
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

// iPhones show a full-screen video natively, outside the Fullscreen API.
function fullScreen() {
  return (
    document.fullscreenElement ||
    document.webkitFullscreenElement ||
    [...document.querySelectorAll("video")].some((video) => video.webkitDisplayingFullscreen)
  )
}

// Each recording starts once at least half of it is on screen and pauses only after it has left the screen entirely
// or a content tab hid it, so a rotated phone that no longer fits the whole recording does not stop it.
// Until then a recording that started is the reader's, so the observer never resumes one they paused.
const onScreen = new IntersectionObserver(
  (entries) => {
    // The page beneath a full-screen video still moves when it enters, rotates, or leaves full screen.
    if (fullScreen()) {
      return
    }
    for (const { target, isIntersecting, intersectionRatio } of entries) {
      if (!isIntersecting) {
        target.pause()
        target.classList.remove("started")
      } else if (intersectionRatio >= 0.5 && !target.classList.contains("started")) {
        // play() rejects when a quick scroll pauses the video before it starts; there is nothing to recover.
        target.play().catch(() => {})
      }
    }
    // Clips that left the screen take a palette switch that waited for them.
    matchPalette()
  },
  { threshold: [0, 0.5] },
)

// Media events do not bubble, so the document catches each recording's start while capturing.
document.addEventListener("play", ({ target }) => target.classList.add("started"), true)

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
