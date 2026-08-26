import '@testing-library/jest-dom/vitest'

// jsdom has no layout, so no scrollIntoView; the page calls it on focus.
Element.prototype.scrollIntoView = () => {}
