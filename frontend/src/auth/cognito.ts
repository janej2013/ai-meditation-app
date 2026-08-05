/**
 * Cognito auth via amazon-cognito-identity-js (SRP; no Amplify).
 *
 * The one rule that matters: every API request carries the **ID token**, never
 * the access token. The HTTP API's JWT authorizer checks `aud`, which Cognito
 * puts only on ID tokens, and the backend additionally rejects any token whose
 * `token_use` is not "id". Sending the access token fails 401 in a way that
 * looks like a login bug, so `getIdToken()` is the only token accessor this
 * module exports.
 */
import {
  AuthenticationDetails,
  CognitoUser,
  CognitoUserAttribute,
  CognitoUserPool,
  CognitoUserSession,
} from 'amazon-cognito-identity-js'

const poolConfig = {
  UserPoolId: import.meta.env.VITE_COGNITO_USER_POOL_ID ?? '',
  ClientId: import.meta.env.VITE_COGNITO_CLIENT_ID ?? '',
}

let pool: CognitoUserPool | null = null

function getPool(): CognitoUserPool {
  if (!pool) {
    if (!poolConfig.UserPoolId || !poolConfig.ClientId) {
      throw new Error('VITE_COGNITO_USER_POOL_ID and VITE_COGNITO_CLIENT_ID must be set')
    }
    pool = new CognitoUserPool(poolConfig)
  }
  return pool
}

function userFor(email: string): CognitoUser {
  return new CognitoUser({ Username: email, Pool: getPool() })
}

/** Sign up; Cognito emails a six-digit confirmation code. */
export function signUp(email: string, password: string): Promise<void> {
  return new Promise((resolve, reject) => {
    getPool().signUp(
      email,
      password,
      [new CognitoUserAttribute({ Name: 'email', Value: email })],
      [],
      (err) => (err ? reject(err) : resolve()),
    )
  })
}

/** Confirm the emailed code. The post-confirmation trigger grants the free credit. */
export function confirmSignUp(email: string, code: string): Promise<void> {
  return new Promise((resolve, reject) => {
    userFor(email).confirmRegistration(code, true, (err) => (err ? reject(err) : resolve()))
  })
}

export function resendCode(email: string): Promise<void> {
  return new Promise((resolve, reject) => {
    userFor(email).resendConfirmationCode((err) => (err ? reject(err) : resolve()))
  })
}

/** SRP sign-in. The session (with its refresh token) lands in localStorage. */
export function signIn(email: string, password: string): Promise<void> {
  return new Promise((resolve, reject) => {
    userFor(email).authenticateUser(
      new AuthenticationDetails({ Username: email, Password: password }),
      {
        onSuccess: () => resolve(),
        onFailure: reject,
        // Not used by this pool, but the callback is required by the API.
        newPasswordRequired: () => reject(new Error('Password change required')),
      },
    )
  })
}

export function signOut(): void {
  getPool().getCurrentUser()?.signOut()
}

function currentSession(): Promise<CognitoUserSession | null> {
  const user = getPool().getCurrentUser()
  if (!user) return Promise.resolve(null)
  return new Promise((resolve) => {
    // getSession refreshes expired tokens with the stored refresh token
    // automatically; it only errors when the refresh token itself is dead,
    // which we treat as signed out rather than an error to surface.
    user.getSession((err: Error | null, session: CognitoUserSession | null) => {
      resolve(err || !session || !session.isValid() ? null : session)
    })
  })
}

/**
 * The ID token for the Authorization header, refreshed if needed.
 * Null means "not signed in" and the caller should route to the sign-in flow.
 */
export async function getIdToken(): Promise<string | null> {
  const session = await currentSession()
  return session ? session.getIdToken().getJwtToken() : null
}

/** The signed-in email, from the ID token's claims (no network call). */
export async function currentEmail(): Promise<string | null> {
  const session = await currentSession()
  return session ? ((session.getIdToken().payload.email as string) ?? null) : null
}

export async function isSignedIn(): Promise<boolean> {
  return (await getIdToken()) !== null
}
