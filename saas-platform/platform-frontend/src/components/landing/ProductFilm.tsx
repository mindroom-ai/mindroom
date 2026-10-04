'use client'

import { useState } from 'react'
import { useDarkMode } from '@/hooks/useDarkMode'

// The films are GitHub attachments so the video files stay out of Git history; next.config's media-src allows their hosts.
const FILMS = {
  light: {
    src: 'https://github.com/user-attachments/assets/383e9556-af82-4b4c-bdd4-8ba894481eec',
    poster: 'https://github.com/user-attachments/assets/e3ce6dae-b730-41cc-a1e9-a7292e8e5766',
  },
  dark: {
    src: 'https://github.com/user-attachments/assets/2c3227cf-0cea-475f-8c27-959a21504348',
    poster: 'https://github.com/user-attachments/assets/70e6d774-5f5c-4d2c-862d-a992136dfd73',
  },
}

export function ProductFilm() {
  const { isDarkMode } = useDarkMode()
  // A film the reader started keeps its theme, so a theme switch never restarts it.
  const [startedFilm, setStartedFilm] = useState<typeof FILMS.light | null>(null)
  const film = startedFilm ?? (isDarkMode ? FILMS.dark : FILMS.light)

  return (
    <video
      src={film.src}
      poster={film.poster}
      controls
      playsInline
      preload="none"
      aria-label="MindRoom product film"
      onPlay={() => setStartedFilm(film)}
      className="aspect-video w-full rounded-lg border border-gray-200 bg-gray-100 shadow-sm dark:border-gray-800 dark:bg-gray-900"
    />
  )
}
