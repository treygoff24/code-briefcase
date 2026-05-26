import {
  type ActiveIntakeRequest,
  isTerminalRunStatus,
} from "@llm-council/contracts";

export class CouncilRuntime {
  async sendIntakeMessages({
    messages,
    requestBody,
  }: {
    messages: unknown[];
    requestBody: ActiveIntakeRequest;
  }): Promise<void> {
    if (isTerminalRunStatus(requestBody.status)) {
      return;
    }
    void messages;
  }
}
