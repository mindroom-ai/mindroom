'use client'

import { useRef, useState } from 'react'
import { Play } from 'lucide-react'
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
  const videoRef = useRef<HTMLVideoElement>(null)
  // A film the reader started keeps its theme, so a theme switch never restarts it.
  const [startedFilm, setStartedFilm] = useState<typeof FILMS.light | null>(null)
  const film = startedFilm ?? (isDarkMode ? FILMS.dark : FILMS.light)
  const started = startedFilm !== null

  return (
    <div className="relative overflow-hidden rounded-2xl bg-gray-100 shadow-[0_24px_60px_-24px_rgba(47,33,97,0.3)] dark:bg-gray-900 dark:shadow-none dark:outline dark:outline-1 dark:outline-white/10">
      <video
        ref={videoRef}
        src={film.src}
        poster={film.poster}
        controls={started}
        playsInline
        preload="none"
        aria-label="MindRoom product film"
        onPlay={() => setStartedFilm(film)}
        className="block aspect-video w-full"
      />
      {!started && (
        <button
          type="button"
          // If the browser refuses to play, hand the reader the native controls instead of a dead button.
          onClick={() => videoRef.current?.play().catch(() => setStartedFilm(film))}
          className="group absolute inset-0 flex items-center justify-center bg-gradient-to-t from-gray-950/25 via-transparent to-transparent"
        >
          <span className="flex items-center gap-3 rounded-full bg-brand-600 py-3 pl-4 pr-6 text-sm font-semibold text-white shadow-[0_12px_30px_-8px_rgba(47,33,97,0.6)] transition-transform duration-300 ease-out group-hover:scale-105 group-active:scale-100 dark:bg-brand-400 dark:text-brand-950">
            <span className="flex h-9 w-9 items-center justify-center rounded-full bg-white/20">
              <Play className="h-4 w-4 translate-x-px fill-current" />
            </span>
            Play the MindRoom film
            <span aria-hidden="true" className="font-normal opacity-80">0:49</span>
          </span>
        </button>
      )}
    </div>
  )
}
