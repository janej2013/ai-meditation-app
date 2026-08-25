/**
 * The account entry, present on every screen: the prototype's credit pill,
 * lifted out of the home panel's header into the app shell so it is always
 * in the top-right corner. Signed out it reads "Sign in" and opens the
 * sign-in flow; signed in it shows the credits left and opens the account.
 *
 * It re-reads the account on every route change: credits move when a
 * session is generated or a pack is bought, and both end in a navigation.
 */
import { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { getAccount } from '../api/client'
import { isSignedIn } from '../auth/cognito'

export default function AccountPill() {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const [signedIn, setSignedIn] = useState(false)
  const [label, setLabel] = useState('Sign in')

  useEffect(() => {
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
      onClick={() => navigate(signedIn ? '/account' : '/signup', { state: { resume: true } })}
    >
      {label}
    </button>
  )
}
