interface MindRoomLogoProps {
  className?: string
  size?: number
}

/** Render the MindRoom currentColor logo at a fixed square size. */
export function MindRoomLogo({ className = '', size = 32 }: MindRoomLogoProps) {
  return (
    <svg
      role="img"
      aria-label="MindRoom logo"
      width={size}
      height={size}
      viewBox="0 0 32 32"
      className={className}
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      <rect width="32" height="32" rx="8" fill="currentColor" />
      <path
        d="M9 21V11.6C9 10.7163 9.71634 10 10.6 10H21.4C22.2837 10 23 10.7163 23 11.6V18.1C23 18.9837 22.2837 19.7 21.4 19.7H15.2L10.8 23V19.7H10.6C9.71634 19.7 9 18.9837 9 18.1"
        fill="white"
      />
      <circle cx="13" cy="15" r="1.2" fill="currentColor" />
      <circle cx="16" cy="15" r="1.2" fill="currentColor" />
      <circle cx="19" cy="15" r="1.2" fill="currentColor" />
    </svg>
  )
}
