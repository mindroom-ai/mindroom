'use client'

import { useState } from 'react'
import { useDarkMode } from '@/hooks/useDarkMode'

// The films are GitHub attachments so the video files stay out of Git history; next.config's media-src allows their hosts.
const FILMS = {
  light: {
    src: 'https://github.com/user-attachments/assets/f35866c4-c226-408a-9f1f-b7588f76c564',
    poster: 'https://github.com/user-attachments/assets/88accc8b-6666-494b-a415-6f7effaaf751',
  },
  dark: {
    src: 'https://github.com/user-attachments/assets/f99556da-f289-49fe-a365-7c2a8bb93c77',
    poster: 'https://github.com/user-attachments/assets/7c8011bd-22b7-4795-8d4d-ab2864c7b118',
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
