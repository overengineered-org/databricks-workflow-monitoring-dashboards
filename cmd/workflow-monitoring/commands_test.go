package main

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

func TestConfigurationLifecycle(t *testing.T) {
	t.Parallel()
	configurationPath := filepath.Join(t.TempDir(), "workflow-monitoring.yml")

	executeCommand(t, configurationPath, "init", "--workspace-id", "123456789")
	executeCommand(
		t,
		configurationPath,
		"add",
		"--job-id",
		"42",
		"--completion-time",
		"06:00",
	)
	executeCommand(
		t,
		configurationPath,
		"update",
		"42",
		"--status",
		"active",
		"--frequency",
		"weekly",
		"--completion-time",
		"07:00",
		"--day-of-week",
		"monday",
	)

	listOutput := executeCommand(t, configurationPath, "list")
	compactListOutput := strings.Join(strings.Fields(listOutput), " ")
	if !strings.Contains(compactListOutput, "42 active weekly 07:00") {
		t.Fatalf("list output did not contain updated workflow:\n%s", listOutput)
	}

	configurationYAML, readError := os.ReadFile(configurationPath)
	if readError != nil {
		t.Fatal(readError)
	}
	if !bytes.HasPrefix(configurationYAML, []byte(schemaHeader)) {
		t.Fatalf("configuration is missing schema header:\n%s", configurationYAML)
	}
	configuration := monitoringConfiguration{}
	if parseError := yaml.Unmarshal(configurationYAML, &configuration); parseError != nil {
		t.Fatal(parseError)
	}
	if len(configuration.Workflows) != 1 {
		t.Fatalf("expected one workflow, got %d", len(configuration.Workflows))
	}
	if configuration.CollectorStorage != nil {
		t.Fatalf("expected automatic storage, got %#v", configuration.CollectorStorage)
	}
	workflow := configuration.Workflows[0]
	if workflow.JobID != 42 || workflow.MonitoringStatus != "active" {
		t.Fatalf("unexpected workflow: %#v", workflow)
	}
	if workflow.SLA.Frequency != "weekly" || workflow.SLA.DayOfWeek != "monday" {
		t.Fatalf("unexpected SLA: %#v", workflow.SLA)
	}

	executeCommand(t, configurationPath, "remove", "42", "--yes")
	configurationDocument, loadError := loadConfigurationDocument(configurationPath)
	if loadError != nil {
		t.Fatal(loadError)
	}
	if len(configurationDocument.configuration.Workflows) != 0 {
		t.Fatalf(
			"expected no workflows, got %d",
			len(configurationDocument.configuration.Workflows),
		)
	}
}

func TestCLIExitCodes(t *testing.T) {
	t.Parallel()
	testCases := []struct {
		name               string
		commandArguments   []string
		expectedExitCode   int
		expectedErrorText  string
		expectedOutputText string
	}{
		{
			name:              "invalid usage",
			commandArguments:  []string{"init"},
			expectedExitCode:  2,
			expectedErrorText: "--workspace-id",
		},
		{
			name: "runtime failure",
			commandArguments: []string{
				"--config",
				filepath.Join(t.TempDir(), "missing.yml"),
				"list",
			},
			expectedExitCode:  1,
			expectedErrorText: "read",
		},
		{
			name:               "help",
			commandArguments:   []string{"--help"},
			expectedExitCode:   0,
			expectedOutputText: "Usage:",
		},
	}
	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			var standardOutput bytes.Buffer
			var errorOutput bytes.Buffer
			exitCode := runCLI(
				testCase.commandArguments,
				&standardOutput,
				&errorOutput,
			)
			if exitCode != testCase.expectedExitCode {
				t.Fatalf(
					"expected exit code %d, got %d; stderr: %s",
					testCase.expectedExitCode,
					exitCode,
					errorOutput.String(),
				)
			}
			if testCase.expectedErrorText != "" &&
				!strings.Contains(errorOutput.String(), testCase.expectedErrorText) {
				t.Fatalf(
					"stderr %q does not contain %q",
					errorOutput.String(),
					testCase.expectedErrorText,
				)
			}
			if testCase.expectedOutputText != "" &&
				!strings.Contains(standardOutput.String(), testCase.expectedOutputText) {
				t.Fatalf(
					"stdout %q does not contain %q",
					standardOutput.String(),
					testCase.expectedOutputText,
				)
			}
		})
	}
}

func TestInitRequiresForceToReplaceConfiguration(t *testing.T) {
	t.Parallel()
	configurationPath := filepath.Join(t.TempDir(), "workflow-monitoring.yml")
	executeCommand(t, configurationPath, "init", "--workspace-id", "123456789")

	_, commandError := runCommand(
		configurationPath,
		"init",
		"--workspace-id",
		"987654321",
	)
	if commandError == nil || !errors.Is(commandError, errInvalidUsage) {
		t.Fatalf("expected replacement usage error, got %v", commandError)
	}
	executeCommand(
		t,
		configurationPath,
		"init",
		"--workspace-id",
		"987654321",
		"--force",
	)
	document, loadError := loadConfigurationDocument(configurationPath)
	if loadError != nil {
		t.Fatal(loadError)
	}
	if document.configuration.WorkspaceID != "987654321" {
		t.Fatalf("configuration was not replaced: %#v", document.configuration)
	}
}

func TestAddRejectsDuplicateJobID(t *testing.T) {
	t.Parallel()
	configurationPath := filepath.Join(t.TempDir(), "workflow-monitoring.yml")
	executeCommand(t, configurationPath, "init", "--workspace-id", "123456789")
	executeCommand(
		t,
		configurationPath,
		"add",
		"--job-id",
		"42",
		"--completion-time",
		"06:00",
	)
	originalConfiguration, readError := os.ReadFile(configurationPath)
	if readError != nil {
		t.Fatal(readError)
	}

	_, commandError := runCommand(
		configurationPath,
		"add",
		"--job-id",
		"42",
		"--completion-time",
		"06:00",
	)
	if commandError == nil || !errors.Is(commandError, errInvalidUsage) {
		t.Fatalf("expected duplicate job usage error, got %v", commandError)
	}
	unchangedConfiguration, readError := os.ReadFile(configurationPath)
	if readError != nil {
		t.Fatal(readError)
	}
	if !bytes.Equal(originalConfiguration, unchangedConfiguration) {
		t.Fatal("rejected duplicate changed the configuration")
	}
}

func TestInitSelectsExistingStorage(t *testing.T) {
	t.Parallel()
	configurationPath := filepath.Join(t.TempDir(), "workflow-monitoring.yml")
	executeCommand(
		t,
		configurationPath,
		"init",
		"--workspace-id",
		"123456789",
		"--catalog",
		"monitoring",
		"--schema",
		"lakeflow_jobs",
	)

	document, loadError := loadConfigurationDocument(configurationPath)
	if loadError != nil {
		t.Fatal(loadError)
	}
	storage := document.configuration.CollectorStorage
	if storage == nil || storage.Catalog != "monitoring" || storage.Schema != "lakeflow_jobs" {
		t.Fatalf("unexpected storage configuration: %#v", storage)
	}
}

func TestInitialConfigurationStorageModes(t *testing.T) {
	t.Parallel()
	testCases := []struct {
		name            string
		workspaceID     string
		catalogName     string
		schemaName      string
		expectsStorage  bool
		expectsUsageErr bool
	}{
		{
			name:        "automatic storage",
			workspaceID: "123456789",
		},
		{
			name:           "existing storage",
			workspaceID:    "123456789",
			catalogName:    "monitoring",
			schemaName:     "lakeflow_jobs",
			expectsStorage: true,
		},
		{
			name:            "partial storage",
			workspaceID:     "123456789",
			catalogName:     "monitoring",
			expectsUsageErr: true,
		},
		{
			name:            "invalid workspace",
			workspaceID:     "workspace-name",
			expectsUsageErr: true,
		},
	}
	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			configuration, configurationError := initialConfiguration(
				testCase.workspaceID,
				"UTC",
				testCase.catalogName,
				testCase.schemaName,
			)
			if testCase.expectsUsageErr {
				if !errors.Is(configurationError, errInvalidUsage) {
					t.Fatalf("expected usage error, got %v", configurationError)
				}
				return
			}
			if configurationError != nil {
				t.Fatal(configurationError)
			}
			if (configuration.CollectorStorage != nil) != testCase.expectsStorage {
				t.Fatalf(
					"unexpected storage configuration: %#v",
					configuration.CollectorStorage,
				)
			}
		})
	}
}

func TestUpdateRequiresChangedField(t *testing.T) {
	t.Parallel()
	configurationPath := filepath.Join(t.TempDir(), "workflow-monitoring.yml")
	executeCommand(t, configurationPath, "init", "--workspace-id", "123456789")
	executeCommand(
		t,
		configurationPath,
		"add",
		"--job-id",
		"42",
		"--completion-time",
		"06:00",
	)

	_, commandError := runCommand(configurationPath, "update", "42")
	if commandError == nil || !errors.Is(commandError, errInvalidUsage) {
		t.Fatalf("expected update usage error, got %v", commandError)
	}
}

func TestUpdateFrequencyClearsOldScheduleFields(t *testing.T) {
	t.Parallel()
	configurationPath := filepath.Join(t.TempDir(), "workflow-monitoring.yml")
	executeCommand(t, configurationPath, "init", "--workspace-id", "123456789")
	executeCommand(
		t,
		configurationPath,
		"add",
		"--job-id",
		"42",
		"--frequency",
		"weekly",
		"--completion-time",
		"06:00",
		"--day-of-week",
		"monday",
	)
	executeCommand(
		t,
		configurationPath,
		"update",
		"42",
		"--frequency",
		"monthly",
		"--day-of-month",
		"31",
	)

	document, loadError := loadConfigurationDocument(configurationPath)
	if loadError != nil {
		t.Fatal(loadError)
	}
	sla := document.configuration.Workflows[0].SLA
	if sla.Frequency != "monthly" || sla.DayOfMonth != 31 || sla.DayOfWeek != "" {
		t.Fatalf("old schedule fields were not cleared: %#v", sla)
	}
}

func TestExistingCommentsSurviveMutations(t *testing.T) {
	t.Parallel()
	configurationPath := filepath.Join(t.TempDir(), "workflow-monitoring.yml")
	configurationYAML := schemaHeader + `# Account configuration.
version: 2
workspace_id: "123456789"
default_timezone: UTC
workflows:
  # Existing workflow comment.
  - job_id: 1
    monitoring_status: inactive
    sla:
      frequency: daily
      completion_time: "06:00"
`
	if writeError := os.WriteFile(
		configurationPath,
		[]byte(configurationYAML),
		0o644,
	); writeError != nil {
		t.Fatal(writeError)
	}
	executeCommand(
		t,
		configurationPath,
		"add",
		"--job-id",
		"2",
		"--completion-time",
		"07:00",
	)
	executeCommand(t, configurationPath, "update", "1", "--status", "active")

	updatedYAML, readError := os.ReadFile(configurationPath)
	if readError != nil {
		t.Fatal(readError)
	}
	expectedComments := []string{
		"# Account configuration.",
		"# Existing workflow comment.",
	}
	for _, expectedComment := range expectedComments {
		if !bytes.Contains(updatedYAML, []byte(expectedComment)) {
			t.Fatalf("missing preserved comment %q:\n%s", expectedComment, updatedYAML)
		}
	}
}

func TestScheduleValidation(t *testing.T) {
	t.Parallel()
	testCases := []struct {
		name      string
		sla       serviceLevelAgreement
		isAllowed bool
	}{
		{name: "daily", sla: serviceLevelAgreement{Frequency: "daily"}, isAllowed: true},
		{
			name: "weekly",
			sla: serviceLevelAgreement{
				Frequency: "weekly",
				DayOfWeek: "monday",
			},
			isAllowed: true,
		},
		{
			name: "fortnightly",
			sla: serviceLevelAgreement{
				Frequency:         "fortnightly",
				FirstDeadlineDate: "2026-09-01",
			},
			isAllowed: true,
		},
		{
			name: "monthly",
			sla: serviceLevelAgreement{
				Frequency:  "monthly",
				DayOfMonth: 31,
			},
			isAllowed: true,
		},
		{
			name: "weekly missing day",
			sla:  serviceLevelAgreement{Frequency: "weekly"},
		},
		{
			name: "fortnightly invalid date",
			sla: serviceLevelAgreement{
				Frequency:         "fortnightly",
				FirstDeadlineDate: "2026-02-30",
			},
		},
		{
			name: "monthly invalid day",
			sla: serviceLevelAgreement{
				Frequency:  "monthly",
				DayOfMonth: 32,
			},
		},
		{
			name: "daily with weekly field",
			sla: serviceLevelAgreement{
				Frequency: "daily",
				DayOfWeek: "monday",
			},
		},
	}
	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			validationError := validateSchedule(testCase.sla)
			if testCase.isAllowed && validationError != nil {
				t.Fatalf("expected valid schedule, got %v", validationError)
			}
			if !testCase.isAllowed && !errors.Is(validationError, errInvalidUsage) {
				t.Fatalf("expected usage error, got %v", validationError)
			}
		})
	}
}

func executeCommand(
	t *testing.T,
	configurationPath string,
	commandArguments ...string,
) string {
	t.Helper()
	commandOutput, commandError := runCommand(configurationPath, commandArguments...)
	if commandError != nil {
		t.Fatalf(
			"command %v failed: %v\n%s",
			commandArguments,
			commandError,
			commandOutput,
		)
	}
	return commandOutput
}

func runCommand(
	configurationPath string,
	commandArguments ...string,
) (string, error) {
	var commandOutput bytes.Buffer
	rootCommand := newRootCommand()
	rootCommand.SetArgs(
		append([]string{"--config", configurationPath}, commandArguments...),
	)
	rootCommand.SetOut(&commandOutput)
	rootCommand.SetErr(&commandOutput)
	commandError := rootCommand.Execute()
	return commandOutput.String(), commandError
}
