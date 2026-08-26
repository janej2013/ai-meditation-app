/**
 * The home flow's decision tree: the sentence opens the panel, chips (or own
 * words) gate Begin drifting, the destination folds into the mood string, and
 * the API's 402/429 behaviours route the way the UI promises.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { SceneProvider } from '../scene/SceneContext'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    getAccount: vi.fn(),
    startGeneration: vi.fn(),
    uploadPicture: vi.fn(),
    describeUploadedPicture: vi.fn(),
  }
})
vi.mock('../auth/cognito', () => ({
  isSignedIn: vi.fn(),
}))
// jsdom has no createImageBitmap or canvas encoder; the normaliser is its own
// unit and here it only needs to hand back a blob.
vi.mock('../picture/prepare', () => ({
  prepareJpeg: vi.fn(),
}))

import {
  ApiError,
  describeUploadedPicture,
  getAccount,
  startGeneration,
  uploadPicture,
} from '../api/client'
import { isSignedIn } from '../auth/cognito'
import { prepareJpeg } from '../picture/prepare'
import HomePage from './HomePage'

function renderHome() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <SceneProvider>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/plans" element={<div>PLANS SCREEN</div>} />
          <Route path="/signup" element={<div>SIGNUP SCREEN</div>} />
          <Route path="/generating/:jobId" element={<div>GENERATING SCREEN</div>} />
          <Route path="/companion" element={<div>COMPANION SCREEN</div>} />
        </Routes>
      </SceneProvider>
    </MemoryRouter>,
  )
}

async function openPanel() {
  const { userEvent } = await import('@testing-library/user-event')
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: 'words' }))
  return user
}

beforeEach(() => {
  // Call history must not leak between tests: several assert "not called".
  vi.clearAllMocks()
  vi.mocked(isSignedIn).mockResolvedValue(true)
  vi.mocked(getAccount).mockResolvedValue({ available: 3, frozen: 0, plan: 'free' })
  // jsdom has no object URLs; the string only ever reaches the
  // (absent-in-tests) particle cloud.
  URL.createObjectURL = vi.fn(() => 'blob:dreamscape')
  URL.revokeObjectURL = vi.fn()
  vi.mocked(prepareJpeg).mockResolvedValue(new Blob(['jpeg'], { type: 'image/jpeg' }))
  vi.mocked(uploadPicture).mockResolvedValue('pic-1')
  vi.mocked(describeUploadedPicture).mockResolvedValue(['dusk', 'ocean', 'longing'])
})

afterEach(() => vi.restoreAllMocks())

describe('HomePage', () => {
  it('starts a generation from a mood chip and moves to the waiting screen', async () => {
    vi.mocked(startGeneration).mockResolvedValue({ job_id: 'j1', status: 'PENDING' })
    renderHome()

    const user = await openPanel()
    await user.click(screen.getByRole('button', { name: 'Stressed' }))
    await user.click(screen.getByRole('button', { name: 'Begin drifting' }))

    await waitFor(() => expect(screen.getByText('GENERATING SCREEN')).toBeInTheDocument())
    expect(startGeneration).toHaveBeenCalledWith({ mood: 'Stressed' }, 10)
  })

  it('folds the destination into the mood string', async () => {
    vi.mocked(startGeneration).mockResolvedValue({ job_id: 'j1', status: 'PENDING' })
    renderHome()

    const user = await openPanel()
    await user.click(screen.getByRole('button', { name: "Can't sleep" }))
    await user.click(screen.getByRole('button', { name: 'Ocean' }))
    await user.click(screen.getByRole('button', { name: 'Begin drifting' }))

    await waitFor(() =>
      expect(startGeneration).toHaveBeenCalledWith(
        { mood: "Can't sleep — drifting to ocean" },
        10,
      ),
    )
  })

  it('accepts a mood in the user’s own words', async () => {
    vi.mocked(startGeneration).mockResolvedValue({ job_id: 'j1', status: 'PENDING' })
    renderHome()

    const user = await openPanel()
    await user.click(screen.getAllByRole('button', { name: 'In my own words…' })[0])
    await user.type(screen.getByPlaceholderText('tired but restless…'), 'tired but restless')
    await user.click(screen.getByRole('button', { name: 'Begin drifting' }))

    await waitFor(() =>
      expect(startGeneration).toHaveBeenCalledWith({ mood: 'tired but restless' }, 10),
    )
  })

  it('routes 402 to the plans screen instead of showing an error', async () => {
    vi.mocked(startGeneration).mockRejectedValue(new ApiError(402, 'No generations remaining.'))
    renderHome()

    const user = await openPanel()
    await user.click(screen.getByRole('button', { name: 'Anxious' }))
    await user.click(screen.getByRole('button', { name: 'Begin drifting' }))

    await waitFor(() => expect(screen.getByText('PLANS SCREEN')).toBeInTheDocument())
  })

  it('surfaces 429 as an in-progress message and stays put', async () => {
    vi.mocked(startGeneration).mockRejectedValue(new ApiError(429, 'Already in progress.'))
    renderHome()

    const user = await openPanel()
    await user.click(screen.getByRole('button', { name: 'Anxious' }))
    await user.click(screen.getByRole('button', { name: 'Begin drifting' }))

    await waitFor(() => expect(screen.getByText(/already being created/i)).toBeInTheDocument())
  })

  it('reads the picture first and starts from its keywords -- no mood asked', async () => {
    vi.mocked(startGeneration).mockResolvedValue({ job_id: 'j1', status: 'PENDING' })
    const { container } = renderHome()
    const { userEvent } = await import('@testing-library/user-event')
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'a picture' }))
    expect(screen.getByText('Let a picture become your dreamscape')).toBeInTheDocument()

    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, {
      target: { files: [new File(['x'], 'sunset.png', { type: 'image/png' })] },
    })

    // The reading arrives; Begin is offered only then, and never a mood chip.
    expect(await screen.findByText('In your picture, we found…')).toBeInTheDocument()
    expect(screen.getByText('longing')).toBeInTheDocument()
    expect(uploadPicture).toHaveBeenCalledTimes(1)
    expect(describeUploadedPicture).toHaveBeenCalledWith('pic-1', expect.anything())
    await user.click(screen.getByRole('button', { name: 'Begin drifting' }))

    await waitFor(() => expect(screen.getByText('GENERATING SCREEN')).toBeInTheDocument())
    expect(startGeneration).toHaveBeenCalledWith({ pictureId: 'pic-1' }, 10)
  })

  it('gates the file dialog on a credit in hand', async () => {
    vi.mocked(getAccount).mockResolvedValue({ available: 0, frozen: 0, plan: 'free' })
    const { container } = renderHome()
    const { userEvent } = await import('@testing-library/user-event')
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'a picture' }))
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    const click = vi.spyOn(input, 'click')

    await user.click(screen.getByRole('button', { name: 'Choose a picture' }))

    await waitFor(() => expect(screen.getByText('PLANS SCREEN')).toBeInTheDocument())
    expect(click).not.toHaveBeenCalled()
  })

  it('sends a signed-out user to signup before the file dialog', async () => {
    vi.mocked(isSignedIn).mockResolvedValue(false)
    renderHome()
    const { userEvent } = await import('@testing-library/user-event')
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'a picture' }))

    await user.click(screen.getByRole('button', { name: 'Choose a picture' }))

    await waitFor(() => expect(screen.getByText('SIGNUP SCREEN')).toBeInTheDocument())
  })

  it('says so when the picture cannot be read, and offers another', async () => {
    vi.mocked(describeUploadedPicture).mockRejectedValue(new ApiError(422, 'unreadable'))
    const { container } = renderHome()
    const { userEvent } = await import('@testing-library/user-event')
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'a picture' }))
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, {
      target: { files: [new File(['x'], 'sunset.png', { type: 'image/png' })] },
    })

    expect(
      await screen.findByText("We couldn't read that picture. Try another one."),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Begin drifting' })).toBeDisabled()
    expect(startGeneration).not.toHaveBeenCalled()
  })

  it('refuses a picture the browser cannot decode', async () => {
    vi.mocked(prepareJpeg).mockRejectedValue(new Error('undecodable'))
    const { container } = renderHome()
    const { userEvent } = await import('@testing-library/user-event')
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'a picture' }))
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, {
      target: { files: [new File(['x'], 'photo.heic', { type: 'image/heic' })] },
    })

    expect(
      await screen.findByText('That picture could not be read. Please choose a JPEG or PNG.'),
    ).toBeInTheDocument()
    expect(uploadPicture).not.toHaveBeenCalled()
  })

  it('sends an unauthenticated user to signup on Begin', async () => {
    vi.mocked(isSignedIn).mockResolvedValue(false)
    renderHome()

    const user = await openPanel()
    await user.click(screen.getByRole('button', { name: 'Restless' }))
    await user.click(screen.getByRole('button', { name: 'Begin drifting' }))

    await waitFor(() => expect(screen.getByText('SIGNUP SCREEN')).toBeInTheDocument())
  })

  it('shows the companion entry locked for a free plan, leading to Pro', async () => {
    renderHome()

    const entry = await screen.findByRole('button', { name: /Talk it through/ })
    await waitFor(() => expect(entry).toHaveTextContent('Part of Pro'))
    expect(entry).toHaveClass('locked')
    fireEvent.click(entry)

    expect(await screen.findByText('PLANS SCREEN')).toBeInTheDocument()
  })

  it('opens the companion for a Pro plan', async () => {
    vi.mocked(getAccount).mockResolvedValue({ available: 12, frozen: 0, plan: 'pro' })
    renderHome()

    const entry = await screen.findByRole('button', { name: /Talk it through/ })
    await waitFor(() => expect(entry).not.toHaveClass('locked'))
    expect(entry).not.toHaveTextContent('Part of Pro')
    fireEvent.click(entry)

    expect(await screen.findByText('COMPANION SCREEN')).toBeInTheDocument()
  })

  it('keeps the entry locked when signed out', async () => {
    vi.mocked(isSignedIn).mockResolvedValue(false)
    renderHome()

    const entry = await screen.findByRole('button', { name: /Talk it through/ })
    expect(entry).toHaveClass('locked')
    expect(getAccount).not.toHaveBeenCalled()
  })
})
