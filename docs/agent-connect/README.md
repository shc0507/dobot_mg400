# External agent integration

This English documentation pack is for an external agent's developer or coding agent. A human starts adaptation explicitly. Complete the connector changes and simulated checks before the external agent operates equipment.

## Read in this order

1. [Project knowledge](project-knowledge.md) explains the Console, equipment, coordinates, samples and operating boundaries. Use it as the operating context.
2. [Communication contract](communication-contract.md) defines discovery, state reads, connection, control, commands, results and compatibility. Use it to implement the connector.

Read additional repository files only when these documents point to a relevant implementation detail. The external agent does not need the repository's development history or a complete source review. Obtain experimental goals and missing physical context from the human.

The [root README](../../README.en.md#connect-an-external-agent) contains the prompt to copy into a coding agent. The [operator guide](../agent-connect.en.md) explains the existing local portal and Console controls.

## Adapt first, operate later

During adaptation, the coding agent changes the external agent's own connector. It implements the documented formats and behavior, then checks them with mocks or the local simulation. It does not change the Console to fit the external agent. This documentation does not authorize physical testing.

Reading the guide in a coding conversation does not give a separate runtime model that knowledge. During adaptation, include the relevant project rules in the external agent's runtime instructions. Supply current measurements separately when the agent makes a decision. Keep connection credentials outside model context.

At runtime, the connector fetches the current definitions and checks that its implemented behavior supports them. It reads current state, action availability, calibrated limits, saved choices and session information. These reads supply current values and detect incompatible changes. They do not request code generation, software installation or autonomous connector changes.

If a change exceeds the connector's supported definitions or meanings, suspend normal command submission and report the mismatch. The human then starts another adaptation task. Keep the independent Stop path available. A compatible update can refresh fields or choices that the connector already supports.

The Console remains authoritative. Its schema, units, control grant, current limits and native device checks determine which requests it accepts. A schema-valid request does not establish a safe physical trajectory or complete an experimental plan.

## Maintain this pack

When Console roles, coordinate meanings or operating boundaries change, update the project knowledge file. When public fields, routes or behavior change, update the communication contract in the same change. Keep both documents factual and current. Store deployment and test history outside this pack.

Keep JSON schemas and action definitions in the Console implementation. This pack explains how to use them and links to their sources. Do not maintain a separate copied catalog of every action or hard-code live calibration values in this pack.

Before publishing an external connector update, report its supported interface versions, relevant action coverage, simulated checks and unresolved assumptions. Do not describe missing metadata or untested equipment behavior as verified.
