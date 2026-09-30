// A palette switch hides one recording of a film; pause it so its narration stops with it.
new MutationObserver(() => {
  for (const video of document.querySelectorAll("video.only-light, video.only-dark")) {
    if (!video.checkVisibility()) {
      video.pause()
    }
  }
}).observe(document.body, { attributeFilter: ["data-md-color-scheme"] })
