/** [Companion · plan_required]: a free account opened /companion directly. */
import { useNavigate } from 'react-router-dom'

export default function PlanRequired() {
  const navigate = useNavigate()
  return (
    <div className="screen plan-required">
      <div className="plan-required-body">
        <div className="plan-required-title">Companion is part of Pro</div>
        <div className="plan-required-sub">
          It remembers what helps you and puts the meditation together with you.
        </div>
      </div>
      <div className="plan-required-actions">
        <button
          className="btn-primary"
          style={{ height: 60, borderRadius: 30, fontSize: 16 }}
          onClick={() => navigate('/plans?plan=plan_pro')}
        >
          See Pro
        </button>
        <button className="btn-ghost" style={{ minHeight: 44 }} onClick={() => navigate('/')}>
          Back
        </button>
      </div>
    </div>
  )
}
