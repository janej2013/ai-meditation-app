/**
 * Turn whatever the user picked into one small JPEG before it goes anywhere.
 *
 * Nova reads JPEG/PNG/WebP/GIF only and bills by resolution, and the upload
 * policy admits a single JPEG under 4 MB — so the browser does the
 * normalising: decode (HEIC included, where the browser can), scale the long
 * edge down, re-encode. Re-encoding through a canvas also drops the file's
 * EXIF block, GPS and all; not a promise, just a side effect the server has no
 * use for anyway.
 */

/** Nova's sweet spot: larger images are downscaled server-side regardless. */
export const MAX_EDGE = 1568
export const JPEG_QUALITY = 0.85

export async function prepareJpeg(file: File): Promise<Blob> {
  // `from-image` applies the EXIF orientation, so a portrait phone photo is
  // not uploaded lying on its side.
  const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' })
  try {
    const scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height))
    const width = Math.max(1, Math.round(bitmap.width * scale))
    const height = Math.max(1, Math.round(bitmap.height * scale))

    const canvas = document.createElement('canvas')
    canvas.width = width
    canvas.height = height
    const ctx = canvas.getContext('2d')
    if (!ctx) throw new Error('canvas unavailable')
    ctx.drawImage(bitmap, 0, 0, width, height)

    const blob = await new Promise<Blob | null>((resolve) =>
      canvas.toBlob(resolve, 'image/jpeg', JPEG_QUALITY),
    )
    if (!blob) throw new Error('could not encode picture')
    return blob
  } finally {
    bitmap.close()
  }
}
