import { readdirSync, readFileSync, statSync } from 'fs'
import { join, relative } from 'path'

const SRC = join(__dirname, '..', '..')

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return name === '__tests__' ? [] : sourceFiles(path)
    return /\.tsx?$/.test(name) ? [path] : []
  })
}

// brand-600 and darker read as dark ink: fine on light surfaces, unreadable on the dark theme's grays.
// Variant prefixes such as focus: or hover: count too; dark: ones are the overrides themselves.
const DARK_INK = /(?<![\w-]|dark:)(text|border|ring)-brand-(600|700|800|900)(?![\w/-])/g

describe('brand colors in dark mode', () => {
  it('gives every dark brand text, border, or ring color a dark-mode override in the same class string', () => {
    const offenders: string[] = []
    for (const file of sourceFiles(SRC)) {
      const literals = readFileSync(file, 'utf8').match(/'[^'\n]*'|"[^"\n]*"|`[^`]*`/g) ?? []
      for (const literal of literals) {
        for (const [, kind, shade] of literal.matchAll(DARK_INK)) {
          const lightInBothThemes = /(?<![\w:-])bg-white(?![\w/-])/.test(literal) && !literal.includes('dark:bg-')
          if (!new RegExp(`dark:(?:[a-z-]+:)?${kind}-`).test(literal) && !literal.includes('onDark') && !lightInBothThemes) {
            offenders.push(`${relative(SRC, file)}: ${kind}-brand-${shade} in ${literal.slice(0, 80)}`)
          }
        }
      }
    }
    expect(offenders).toEqual([])
  })
})
