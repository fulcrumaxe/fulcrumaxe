/**
 * scenario_validate.test.ts — JSON schema validation for loop-controller scenario files.
 *
 * Validates that every *.scenario.json in dashboard/scenarios/loop-controller/ has the
 * required keys and correct shape. Uses a lightweight inline schema — no extra deps.
 */

import * as fs from 'fs'
import * as path from 'path'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const SCENARIO_DIR = path.resolve(__dirname, '..')

// ---------------------------------------------------------------------------
// Required keys for a valid scenario file
// ---------------------------------------------------------------------------
const REQUIRED_KEYS = ['name', 'goal', 'url', 'steps', 'success_criteria'] as const

interface ScenarioStep {
  action: string
  [key: string]: unknown
}

interface SuccessCriterion {
  kind: string
  [key: string]: unknown
}

interface Scenario {
  name: string
  goal: string
  url: string
  steps: ScenarioStep[]
  success_criteria: SuccessCriterion[]
  [key: string]: unknown
}

function validateScenario(filePath: string): { valid: boolean; errors: string[] } {
  const errors: string[] = []

  let raw: string
  try {
    raw = fs.readFileSync(filePath, 'utf8')
  } catch (err) {
    return { valid: false, errors: [`Cannot read file: ${err}`] }
  }

  let data: unknown
  try {
    data = JSON.parse(raw)
  } catch (err) {
    return { valid: false, errors: [`JSON parse error: ${err}`] }
  }

  if (typeof data !== 'object' || data === null || Array.isArray(data)) {
    return { valid: false, errors: ['Root value must be a JSON object'] }
  }

  const obj = data as Record<string, unknown>

  // Check required keys
  for (const key of REQUIRED_KEYS) {
    if (!(key in obj)) {
      errors.push(`Missing required key: "${key}"`)
    }
  }

  if (errors.length > 0) return { valid: false, errors }

  const scenario = obj as unknown as Scenario

  // name: non-empty string
  if (typeof scenario.name !== 'string' || scenario.name.trim() === '') {
    errors.push('"name" must be a non-empty string')
  }

  // goal: non-empty string
  if (typeof scenario.goal !== 'string' || scenario.goal.trim() === '') {
    errors.push('"goal" must be a non-empty string')
  }

  // url: string starting with http
  if (typeof scenario.url !== 'string' || !scenario.url.startsWith('http')) {
    errors.push('"url" must be a string starting with "http"')
  }

  // steps: non-empty array of objects with "action" key
  if (!Array.isArray(scenario.steps) || scenario.steps.length === 0) {
    errors.push('"steps" must be a non-empty array')
  } else {
    scenario.steps.forEach((step, i) => {
      if (typeof step !== 'object' || step === null || Array.isArray(step)) {
        errors.push(`steps[${i}] must be an object`)
      } else if (typeof step.action !== 'string' || step.action.trim() === '') {
        errors.push(`steps[${i}].action must be a non-empty string`)
      }
    })
  }

  // success_criteria: non-empty array of objects with "kind" key
  if (!Array.isArray(scenario.success_criteria) || scenario.success_criteria.length === 0) {
    errors.push('"success_criteria" must be a non-empty array')
  } else {
    scenario.success_criteria.forEach((criterion, i) => {
      if (typeof criterion !== 'object' || criterion === null || Array.isArray(criterion)) {
        errors.push(`success_criteria[${i}] must be an object`)
      } else if (typeof criterion.kind !== 'string' || criterion.kind.trim() === '') {
        errors.push(`success_criteria[${i}].kind must be a non-empty string`)
      }
    })
  }

  return { valid: errors.length === 0, errors }
}

// ---------------------------------------------------------------------------
// Discover all *.scenario.json files in the parent directory
// ---------------------------------------------------------------------------
const scenarioFiles = fs
  .readdirSync(SCENARIO_DIR)
  .filter(f => f.endsWith('.scenario.json'))
  .map(f => path.join(SCENARIO_DIR, f))

describe('loop-controller scenario files', () => {
  it('should find at least one scenario file', () => {
    expect(scenarioFiles.length).toBeGreaterThanOrEqual(1)
  })

  it('should include the three required scenarios (load-page, start-loop-button, view-iteration-history)', () => {
    const names = scenarioFiles.map(f => path.basename(f))
    expect(names).toContain('load-page.scenario.json')
    expect(names).toContain('start-loop-button.scenario.json')
    expect(names).toContain('view-iteration-history.scenario.json')
  })

  scenarioFiles.forEach(filePath => {
    const fileName = path.basename(filePath)

    describe(fileName, () => {
      let result: ReturnType<typeof validateScenario>

      beforeAll(() => {
        result = validateScenario(filePath)
      })

      it('should be valid JSON with all required keys', () => {
        if (!result.valid) {
          throw new Error(`Validation failed:\n  ${result.errors.join('\n  ')}`)
        }
      })

      it('should have a kebab-case name matching the filename', () => {
        const raw = fs.readFileSync(filePath, 'utf8')
        const data = JSON.parse(raw) as { name?: string }
        const expectedName = fileName.replace(/\.scenario\.json$/, '')
        expect(data.name).toBe(expectedName)
      })
    })
  })
})
