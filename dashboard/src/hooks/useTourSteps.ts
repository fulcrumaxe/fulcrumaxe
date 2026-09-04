/**
 * useTourSteps — static tour step definitions for the onboarding tour.
 *
 * Each step targets a DOM element via a CSS selector (data-tour attribute).
 * Steps span 4 primary pages; OnboardingTour navigates between routes as needed.
 */

export interface TourStep {
  id: string
  route: string
  selector: string
  title: string
  body: string
  placement: 'top' | 'bottom' | 'left' | 'right'
}

const TOUR_STEPS: TourStep[] = [
  {
    id: 'home-status',
    route: '/',
    selector: '[data-tour="home-status"]',
    title: 'Project overview',
    body: 'Your project list lives here. Pick a project to dive into its dashboard — budget, KPIs, and live agent activity.',
    placement: 'bottom',
  },
  {
    id: 'loop-runner',
    route: '/loop-controller',
    selector: '[data-tour="loop-runner"]',
    title: 'Loop runner',
    body: 'Triggers a full /loop iteration — the team scans GitHub, spawns agents, and merges PRs. Watch output stream in real time.',
    placement: 'bottom',
  },
  {
    id: 'pr-inspector',
    route: '/prs',
    selector: '[data-tour="pr-inspector"]',
    title: 'PR inspector',
    body: 'Every open pull request at a glance. Filter by stuck or ready-to-merge, see fix-cycle counts and gate labels.',
    placement: 'bottom',
  },
  {
    id: 'loop-timeline',
    route: '/loop-timeline',
    selector: '[data-tour="loop-timeline"]',
    title: 'Loop timeline',
    body: 'Duration and activity charts across all loop iterations. Click any data point to read the full run log.',
    placement: 'top',
  },
  {
    id: 'stats-tile',
    route: '/stats',
    selector: '[data-tour="stats-tile"]',
    title: 'Team metrics',
    body: 'Per-merge metrics with 7-day sparklines — cycle time, fix rounds, cost per PR, and more.',
    placement: 'bottom',
  },
  {
    id: 'settings-nav',
    route: '/',
    selector: '[data-tour="settings-nav"]',
    title: 'Settings & replay',
    body: 'Configure control-plane gates here. You can also restart this tour from Settings at any time.',
    placement: 'bottom',
  },
]

export function useTourSteps(): TourStep[] {
  return TOUR_STEPS
}
