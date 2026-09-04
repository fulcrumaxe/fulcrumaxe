/**
 * Type re-exports for stats tile interfaces used by other modules.
 *
 * Component re-exports have been removed — tiles are now auto-discovered
 * via the registry (stats/registry.ts). Add types here when a tile's
 * response type needs to be imported outside the stats directory.
 */

export type { CostSpikeResponse } from './CostSpikesTile'
export type { CosmeticBlocksResponse } from './CosmeticBlocksTile'
export type { TeamLeadTokensResponse } from './TeamLeadTokensTile'
export type { RoleSuccessRow, RoleRetryRow } from './RoleSuccessRateTile'
export type { IdleRatioResponse } from './LoopIdleRatioTile'
export type { FixRoundsResponse } from './AvgFixRoundsTile'
export type { PreWriteBurnRow, PreWriteBurnResponse } from './PreWriteBurnTile'
export type { VerdictOverturnRow } from './VerdictOverturnTile'
