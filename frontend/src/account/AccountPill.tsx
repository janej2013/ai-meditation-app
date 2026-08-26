/**
 * The account entry, present on every screen: the prototype's credit pill,
 * lifted out of the home panel's header into the app shell so it is always
 * in the top-right corner. Signed out it reads "Sign in" and opens the
 * sign-in flow; signed in it shows the credits left and opens the account.
 *
 * Two rules. It reads once on whatever route the app opened on -- a reload
 * on the companion or the waiting screen must not leave a signed-in user at
 * "Sign in" -- and it re-reads on the routes a credit change lands on: home,
 * the player, the account and plans screens, the collection. It does not
 * re-read on the waiting screen, where the read would race the freeze; a
 * reload there can still show the pre-freeze number until the player.
 */
import { useEffect, useState, type Dispatch, type SetStateAction } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { getAccount } from '../api/client'
import { isSignedIn } from '../auth/cognito'

const REFRESH_ON = /^\/(|account|plans|dreamscapes|player\/.*)$/

/** One read of the account; returns its cancel, for an effect's cleanup. */
function read(
  setSignedIn: Dispatch<SetStateAction<boolean>>,
  setLabel: Dispatch<SetStateAction<string>>,
): () => void {
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
}

export default function AccountPill() {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const [signedIn, setSignedIn] = useState(false)
  const [label, setLabel] = useState('Sign in')

  // Each effect is safe to run twice (StrictMode's dev double-invoke): a
  // cancelled first run is simply repeated by the second. No "first mount"
  // ref -- one would survive the simulated remount and skip the read.
  useEffect(() => {
    if (!REFRESH_ON.test(pathname)) return read(setSignedIn, setLabel)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the opening route only
  }, [])
  useEffect(() => {
    if (REFRESH_ON.test(pathname)) return read(setSignedIn, setLabel)
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
