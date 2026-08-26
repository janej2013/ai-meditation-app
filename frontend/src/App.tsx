import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import AccountPill from './account/AccountPill'
import AccountPage from './pages/AccountPage'
import CompanionPage from './companion/CompanionPage'
import DreamscapesPage from './pages/DreamscapesPage'
import FailedPage from './pages/FailedPage'
import GeneratingPage from './pages/GeneratingPage'
import HomePage from './pages/HomePage'
import PlansPage from './pages/PlansPage'
import PlayerPage from './pages/PlayerPage'
import SignupPage from './pages/SignupPage'
import VerifyPage from './pages/VerifyPage'
import { SceneProvider } from './scene/SceneContext'
import SceneLayer from './scene/SceneLayer'

export default function App() {
  return (
    <BrowserRouter>
      <SceneProvider>
        <div className="shell">
          <SceneLayer />
          {/* Above every screen's content: the one account entry. */}
          <AccountPill />
          <div className="content">
            <Routes>
              <Route path="/" element={<HomePage />} />
              <Route path="/generating/:jobId" element={<GeneratingPage />} />
              <Route path="/player/:jobId" element={<PlayerPage />} />
              <Route path="/failed" element={<FailedPage />} />
              <Route path="/plans" element={<PlansPage />} />
              <Route path="/signup" element={<SignupPage />} />
              <Route path="/verify" element={<VerifyPage />} />
              <Route path="/account" element={<AccountPage />} />
              <Route path="/dreamscapes" element={<DreamscapesPage />} />
              <Route path="/companion" element={<CompanionPage />} />
              {/* Stripe return URLs; success lands on the account with a banner. */}
              <Route
                path="/billing/success"
                element={<Navigate to="/account?paid=1" replace />}
              />
              <Route path="/billing/cancel" element={<Navigate to="/plans" replace />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </div>
        </div>
      </SceneProvider>
    </BrowserRouter>
  )
}
