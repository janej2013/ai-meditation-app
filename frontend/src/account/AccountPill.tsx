/**
 * The account entry, present on every screen: the prototype's credit pill,
 * lifted out of the home panel's header into the app shell so it is always
 * in the top-right corner. Signed out it reads "Sign in" and opens the
 * sign-in flow; signed in it shows the credits left and opens the account.
 *
 * It re-reads the account on the routes a credit change lands on -- home,
 * the player, the account and plans screens, the collection -- and not on
 * the waiting screen, where the read would race the freeze and show a stale
 * number anyway.
 */
import { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { getAccount } from '../api/client'
import { isSignedIn } from '../auth/cognito'

const REFRESH_ON = /^\/(|account|plans|dreamscapes|player\/.*)$/

export default function AccountPill() {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const [signedIn, setSignedIn] = useState(false)
  const [label, setLabel] = useState('Sign in')

  useEffect(() => {
    if (!REFRESH_ON.test(pathname)) return
    let cancelled = false
    void (async () => {
      if (!(await isSignedIn())) {
        if (!cancelled) {
          setSignedIn(false)
          setLabel('Sign in')
        }
        return
      }
      if (!cancelled) setSignedIn(true)
      try {
        const account = await getAccount()
        if (!cancelled) setLabel(`${account.available} left`)
      } catch {
        if (!cancelled) setLabel('Account')
      }
    })()
    return () => {
      cancelled = true
    }
  }, [pathname])

  return (
    <button
      className="account-pill"
      aria-label={signedIn ? 'Your account' : 'Sign in'}
      onClick={() =>
        signedIn ? navigate('/account') : navigate('/signup', { state: { resume: true } })
      }
    >
      {label}
    </button>
  )
}
