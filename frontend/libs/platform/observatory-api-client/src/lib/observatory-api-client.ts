import {
  OBSERVATORY_API_OPERATIONS,
  type ObservatoryApiOperations,
  type ObservatoryOperationId,
  type ObservatoryOperationInput,
  type ObservatoryOperationResponse,
} from './generated/observatory-api';

export interface ObservatoryTransport {
  execute<K extends ObservatoryOperationId>(
    operation: (typeof OBSERVATORY_API_OPERATIONS)[K],
    input: ObservatoryOperationInput<K>,
  ): Promise<ObservatoryApiOperations[K]['response']>;
}

/** Transport-only facade. Authentication, retries, SSE reconciliation, and feature state belong to consumers. */
export class ObservatoryApiClient {
  constructor(private readonly transport: ObservatoryTransport) {}

  execute<K extends ObservatoryOperationId>(
    operationId: K,
    input: ObservatoryOperationInput<K>,
  ): Promise<ObservatoryOperationResponse<K>> {
    return this.transport.execute(OBSERVATORY_API_OPERATIONS[operationId], input);
  }
}
