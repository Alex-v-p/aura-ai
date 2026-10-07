import {
  AURA_API_OPERATIONS,
  type AuraApiOperations,
  type AuraOperationId,
  type AuraOperationInput,
  type AuraOperationResponse,
} from './generated/aura-api';

export interface AuraTransport {
  execute<K extends AuraOperationId>(
    operation: (typeof AURA_API_OPERATIONS)[K],
    input: AuraOperationInput<K>,
  ): Promise<AuraApiOperations[K]['response']>;
}

/** Transport-only facade. Authentication, state, retries, and reconciliation belong to consumers. */
export class AuraApiClient {
  constructor(private readonly transport: AuraTransport) {}

  execute<K extends AuraOperationId>(
    operationId: K,
    input: AuraOperationInput<K>,
  ): Promise<AuraOperationResponse<K>> {
    return this.transport.execute(AURA_API_OPERATIONS[operationId], input);
  }
}
