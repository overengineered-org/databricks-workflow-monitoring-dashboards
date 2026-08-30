// Package main provides the workflow monitoring configuration CLI.
package main

import (
	"errors"
	"fmt"
	"io"
	"os"
	"strconv"
	"text/tabwriter"

	"github.com/spf13/cobra"
)

var errInvalidUsage = errors.New("invalid usage")

type commandSettings struct {
	configurationPath string
}

type workflowFlags struct {
	jobID             int64
	monitoringStatus  string
	frequency         string
	completionTime    string
	timezone          string
	dayOfWeek         string
	firstDeadlineDate string
	dayOfMonth        int
}

func newRootCommand() *cobra.Command {
	settings := &commandSettings{}
	rootCommand := &cobra.Command{
		Use:           "workflow-monitoring",
		Short:         "Create and maintain workflow-monitoring.yml",
		Version:       buildVersion,
		SilenceErrors: true,
		SilenceUsage:  true,
	}
	rootCommand.SetFlagErrorFunc(func(_ *cobra.Command, err error) error {
		return fmt.Errorf("%w: %v", errInvalidUsage, err)
	})
	rootCommand.PersistentFlags().StringVar(
		&settings.configurationPath,
		"config",
		defaultConfigurationPath,
		"workflow monitoring YAML path",
	)
	rootCommand.AddCommand(
		newInitCommand(settings),
		newListCommand(settings),
		newAddCommand(settings),
		newUpdateCommand(settings),
		newRemoveCommand(settings),
	)
	return rootCommand
}

func newInitCommand(settings *commandSettings) *cobra.Command {
	var workspaceID string
	defaultTimezone := "UTC"
	var catalogName string
	var schemaName string
	var isReplacementApproved bool
	initCommand := &cobra.Command{
		Use:   "init",
		Short: "Create a monitoring configuration",
		Args:  noArguments,
		Example: "  workflow-monitoring init --workspace-id 123456789\n" +
			"  workflow-monitoring init --workspace-id 123456789 --catalog monitoring --schema jobs",
		RunE: func(command *cobra.Command, _ []string) error {
			configuration, validationError := initialConfiguration(
				workspaceID,
				defaultTimezone,
				catalogName,
				schemaName,
			)
			if validationError != nil {
				return validationError
			}
			if _, statError := os.Stat(settings.configurationPath); statError == nil && !isReplacementApproved {
				return usageErrorf(
					"%s already exists; pass --force to replace it",
					settings.configurationPath,
				)
			} else if statError != nil && !errors.Is(statError, os.ErrNotExist) {
				return fmt.Errorf("inspect configuration: %w", statError)
			}
			document, encodeError := newConfigurationDocument(configuration)
			if encodeError != nil {
				return encodeError
			}
			if writeError := document.write(settings.configurationPath); writeError != nil {
				return writeError
			}
			return writeOutput(
				command.OutOrStdout(),
				"Created %s. Next: workflow-monitoring add --help\n",
				settings.configurationPath,
			)
		},
	}
	initCommand.Flags().StringVar(
		&workspaceID,
		"workspace-id",
		"",
		"required Databricks workspace ID",
	)
	initCommand.Flags().StringVar(
		&defaultTimezone,
		"default-timezone",
		defaultTimezone,
		"default IANA SLA timezone",
	)
	initCommand.Flags().StringVar(
		&catalogName,
		"catalog",
		"",
		"existing Unity Catalog catalog",
	)
	initCommand.Flags().StringVar(
		&schemaName,
		"schema",
		"",
		"existing Unity Catalog schema",
	)
	initCommand.Flags().BoolVar(
		&isReplacementApproved,
		"force",
		false,
		"replace an existing configuration",
	)
	return initCommand
}

func newListCommand(settings *commandSettings) *cobra.Command {
	return &cobra.Command{
		Use:   "list",
		Short: "List configured workflows",
		Args:  noArguments,
		RunE: func(command *cobra.Command, _ []string) error {
			document, loadError := loadConfigurationDocument(settings.configurationPath)
			if loadError != nil {
				return loadError
			}
			if len(document.configuration.Workflows) == 0 {
				return writeOutput(command.OutOrStdout(), "No workflows configured.\n")
			}
			tableWriter := tabwriter.NewWriter(
				command.OutOrStdout(),
				0,
				4,
				2,
				' ',
				0,
			)
			if outputError := writeOutput(
				tableWriter,
				"JOB ID\tSTATUS\tSLA\tDEADLINE\n",
			); outputError != nil {
				return outputError
			}
			for _, workflow := range document.configuration.Workflows {
				if outputError := writeOutput(
					tableWriter,
					"%d\t%s\t%s\t%s\n",
					workflow.JobID,
					workflow.MonitoringStatus,
					workflow.SLA.Frequency,
					workflow.SLA.CompletionTime,
				); outputError != nil {
					return outputError
				}
			}
			return tableWriter.Flush()
		},
	}
}

func newAddCommand(settings *commandSettings) *cobra.Command {
	flagValues := &workflowFlags{
		monitoringStatus: "inactive",
		frequency:        "daily",
	}
	addCommand := &cobra.Command{
		Use:   "add",
		Short: "Add one workflow",
		Args:  noArguments,
		Example: "  workflow-monitoring add --job-id 42 --completion-time 06:00\n" +
			"  workflow-monitoring add --job-id 42 --status active --frequency weekly " +
			"--completion-time 06:00 --day-of-week monday",
		RunE: func(command *cobra.Command, _ []string) error {
			document, loadError := loadConfigurationDocument(settings.configurationPath)
			if loadError != nil {
				return loadError
			}
			workflow := flagValues.workflow()
			if validationError := validateWorkflow(
				workflow,
				document.configuration.DefaultTimezone,
			); validationError != nil {
				return validationError
			}
			if document.workflowIndex(workflow.JobID) >= 0 {
				return usageErrorf("job id %d is already configured", workflow.JobID)
			}
			if appendError := document.appendWorkflow(workflow); appendError != nil {
				return appendError
			}
			if writeError := document.write(settings.configurationPath); writeError != nil {
				return writeError
			}
			return writeOutput(
				command.OutOrStdout(),
				"Added job %d. Next: workflow-monitoring list\n",
				workflow.JobID,
			)
		},
	}
	bindWorkflowFlags(addCommand, flagValues, true)
	return addCommand
}

func newUpdateCommand(settings *commandSettings) *cobra.Command {
	flagValues := &workflowFlags{}
	updateCommand := &cobra.Command{
		Use:   "update <job-id>",
		Short: "Update fields on one workflow",
		Args:  exactlyOneArgument,
		Example: "  workflow-monitoring update 42 --status active\n" +
			"  workflow-monitoring update 42 --frequency monthly --day-of-month 31",
		RunE: func(command *cobra.Command, commandArguments []string) error {
			if !hasChangedWorkflowFlag(command) {
				return usageErrorf("provide at least one workflow field to update")
			}
			jobID, parseError := positiveJobID(commandArguments[0])
			if parseError != nil {
				return parseError
			}
			document, loadError := loadConfigurationDocument(settings.configurationPath)
			if loadError != nil {
				return loadError
			}
			workflowIndex := document.workflowIndex(jobID)
			if workflowIndex < 0 {
				return usageErrorf("job id %d is not configured", jobID)
			}
			workflow := document.configuration.Workflows[workflowIndex]
			applyChangedWorkflowFlags(command, &workflow, flagValues)
			if validationError := validateWorkflow(
				workflow,
				document.configuration.DefaultTimezone,
			); validationError != nil {
				return validationError
			}
			if replaceError := document.replaceWorkflow(
				workflowIndex,
				workflow,
			); replaceError != nil {
				return replaceError
			}
			if writeError := document.write(settings.configurationPath); writeError != nil {
				return writeError
			}
			return writeOutput(command.OutOrStdout(), "Updated job %d.\n", jobID)
		},
	}
	bindWorkflowFlags(updateCommand, flagValues, false)
	return updateCommand
}

func newRemoveCommand(settings *commandSettings) *cobra.Command {
	var isRemovalApproved bool
	removeCommand := &cobra.Command{
		Use:   "remove <job-id>",
		Short: "Remove one workflow",
		Args:  exactlyOneArgument,
		RunE: func(command *cobra.Command, commandArguments []string) error {
			if !isRemovalApproved {
				return usageErrorf("pass --yes to remove the workflow")
			}
			jobID, parseError := positiveJobID(commandArguments[0])
			if parseError != nil {
				return parseError
			}
			document, loadError := loadConfigurationDocument(settings.configurationPath)
			if loadError != nil {
				return loadError
			}
			workflowIndex := document.workflowIndex(jobID)
			if workflowIndex < 0 {
				return usageErrorf("job id %d is not configured", jobID)
			}
			document.removeWorkflow(workflowIndex)
			if writeError := document.write(settings.configurationPath); writeError != nil {
				return writeError
			}
			return writeOutput(command.OutOrStdout(), "Removed job %d.\n", jobID)
		},
	}
	removeCommand.Flags().BoolVar(
		&isRemovalApproved,
		"yes",
		false,
		"confirm workflow removal",
	)
	return removeCommand
}

func bindWorkflowFlags(
	command *cobra.Command,
	values *workflowFlags,
	includeJobID bool,
) {
	if includeJobID {
		command.Flags().Int64Var(
			&values.jobID,
			"job-id",
			0,
			"required Databricks Job ID",
		)
	}
	command.Flags().StringVar(
		&values.monitoringStatus,
		"status",
		values.monitoringStatus,
		"active or inactive",
	)
	command.Flags().StringVar(
		&values.frequency,
		"frequency",
		values.frequency,
		"daily, weekly, fortnightly, or monthly",
	)
	command.Flags().StringVar(
		&values.completionTime,
		"completion-time",
		"",
		"required HH:MM deadline",
	)
	command.Flags().StringVar(
		&values.timezone,
		"timezone",
		"",
		"optional IANA timezone override",
	)
	command.Flags().StringVar(
		&values.dayOfWeek,
		"day-of-week",
		"",
		"weekly deadline day",
	)
	command.Flags().StringVar(
		&values.firstDeadlineDate,
		"first-deadline-date",
		"",
		"fortnightly YYYY-MM-DD anchor",
	)
	command.Flags().IntVar(
		&values.dayOfMonth,
		"day-of-month",
		0,
		"monthly deadline day from 1 to 31",
	)
}

func (values *workflowFlags) workflow() monitoredWorkflow {
	return monitoredWorkflow{
		JobID:            values.jobID,
		MonitoringStatus: values.monitoringStatus,
		SLA: serviceLevelAgreement{
			Frequency:         values.frequency,
			CompletionTime:    values.completionTime,
			Timezone:          values.timezone,
			DayOfWeek:         values.dayOfWeek,
			FirstDeadlineDate: values.firstDeadlineDate,
			DayOfMonth:        values.dayOfMonth,
		},
	}
}

func applyChangedWorkflowFlags(
	command *cobra.Command,
	workflow *monitoredWorkflow,
	values *workflowFlags,
) {
	if command.Flags().Changed("status") {
		workflow.MonitoringStatus = values.monitoringStatus
	}
	if command.Flags().Changed("frequency") {
		workflow.SLA.Frequency = values.frequency
		workflow.SLA.DayOfWeek = ""
		workflow.SLA.FirstDeadlineDate = ""
		workflow.SLA.DayOfMonth = 0
	}
	if command.Flags().Changed("completion-time") {
		workflow.SLA.CompletionTime = values.completionTime
	}
	if command.Flags().Changed("timezone") {
		workflow.SLA.Timezone = values.timezone
	}
	if command.Flags().Changed("day-of-week") {
		workflow.SLA.DayOfWeek = values.dayOfWeek
	}
	if command.Flags().Changed("first-deadline-date") {
		workflow.SLA.FirstDeadlineDate = values.firstDeadlineDate
	}
	if command.Flags().Changed("day-of-month") {
		workflow.SLA.DayOfMonth = values.dayOfMonth
	}
}

func noArguments(command *cobra.Command, arguments []string) error {
	if err := cobra.NoArgs(command, arguments); err != nil {
		return fmt.Errorf("%w: %v", errInvalidUsage, err)
	}
	return nil
}

func exactlyOneArgument(command *cobra.Command, arguments []string) error {
	if err := cobra.ExactArgs(1)(command, arguments); err != nil {
		return fmt.Errorf("%w: %v", errInvalidUsage, err)
	}
	return nil
}

func positiveJobID(candidate string) (int64, error) {
	jobID, parseError := strconv.ParseInt(candidate, 10, 64)
	if parseError != nil || jobID <= 0 {
		return 0, usageErrorf("job id must be a positive integer")
	}
	return jobID, nil
}

func usageErrorf(format string, arguments ...any) error {
	return fmt.Errorf("%w: %s", errInvalidUsage, fmt.Sprintf(format, arguments...))
}

func writeOutput(outputWriter io.Writer, format string, arguments ...any) error {
	if _, outputError := fmt.Fprintf(outputWriter, format, arguments...); outputError != nil {
		return fmt.Errorf("write command output: %w", outputError)
	}
	return nil
}

func hasChangedWorkflowFlag(command *cobra.Command) bool {
	workflowFlagNames := []string{
		"status",
		"frequency",
		"completion-time",
		"timezone",
		"day-of-week",
		"first-deadline-date",
		"day-of-month",
	}
	for _, flagName := range workflowFlagNames {
		if command.Flags().Changed(flagName) {
			return true
		}
	}
	return false
}
