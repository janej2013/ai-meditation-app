/**
 * The prototype's screen-entry beat: mount at opacity 0, breathe in over a
 * second. Shared by every screen that arrives this way (player, collection).
 */
import { useEffect, useState } from 'react'

export function useFadeIn(): { transition: string; opacity: number } {
  const [fade, setFade] = useState(0)
  useEffect(() => {
    const t = setTimeout(() => setFade(1), 40)
    return () => clearTimeout(t)
  }, [])
  return { transition: 'opacity 1s ease', opacity: fade }
}
