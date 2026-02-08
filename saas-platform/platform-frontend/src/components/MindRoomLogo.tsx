import Image from 'next/image'

interface MindRoomLogoProps {
  className?: string
  size?: number
}

export function MindRoomLogo({ className = '', size = 32 }: MindRoomLogoProps) {
  return (
    <Image
      src="/brain-logo.svg"
      alt="MindRoom logo"
      width={size}
      height={size}
      className={className}
    />
  )
}
