interface MindRoomLogoProps {
  className?: string
  size?: number
}

/** Render the shared MindRoom brand mark at a fixed square size. */
export function MindRoomLogo({ className = '', size = 32 }: MindRoomLogoProps) {
  return (
    <img
      src="/res/branding/mindroom.svg"
      alt="MindRoom logo"
      width={size}
      height={size}
      className={`object-contain ${className}`}
    />
  )
}
