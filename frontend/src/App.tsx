import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import AccountPage from './pages/AccountPage'
import FailedPage from './pages/FailedPage'
import GeneratingPage from './pages/GeneratingPage'
import HomePage from './pages/HomePage'
import PlansPage from './pages/PlansPage'
import PlayerPage from './pages/PlayerPage'
import SignupPage from './pages/SignupPage'
import VerifyPage from './pages/VerifyPage'

export default function App() {
  return (
    <BrowserRouter>
      <div className="shell">
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/generating/:jobId" element={<GeneratingPage />} />
          <Route path="/player/:jobId" element={<PlayerPage />} />
          <Route path="/failed" element={<FailedPage />} />
          <Route path="/plans" element={<PlansPage />} />
          <Route path="/signup" element={<SignupPage />} />
          <Route path="/verify" element={<VerifyPage />} />
          <Route path="/account" element={<AccountPage />} />
          {/* Stripe return URLs; success lands on the account with a banner. */}
          <Route path="/billing/success" element={<Navigate to="/account?paid=1" replace />} />
          <Route path="/billing/cancel" element={<Navigate to="/plans" replace />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </div>
    </BrowserRouter>
  )
}
