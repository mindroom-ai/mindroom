// Painted stock avatars from mindroom-ai/assets, resized to 160px WebP in public/avatars.
export type AvatarName =
  | 'analyst'
  | 'builder'
  | 'code'
  | 'computer'
  | 'data'
  | 'email'
  | 'general'
  | 'helper'
  | 'home'
  | 'mind'
  | 'news'
  | 'phone'
  | 'planner'
  | 'research'
  | 'router'
  | 'security'
  | 'summary'
  | 'writer'

export function AgentAvatar({ name, size = 32, className = '' }: { name: AvatarName; size?: number; className?: string }) {
  return (
    <img
      src={`/avatars/${name}.webp`}
      alt=""
      width={size}
      height={size}
      decoding="async"
      style={{ width: size, height: size }}
      className={`shrink-0 rounded-[28%] object-cover ${className}`}
    />
  )
}
