import { Brain } from 'lucide-react'

interface MindRoomLogoProps {
  className?: string
  size?: number
}

export function MindRoomLogo({ className = '', size = 32 }: MindRoomLogoProps) {
  return (
    <Brain className={className} size={size} />
  )
}
