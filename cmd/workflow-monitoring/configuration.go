package main

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"time"

	"github.com/google/renameio/v2/maybe"
	"gopkg.in/yaml.v3"
)

const (
	defaultConfigurationPath = "workflow-monitoring.yml"
	configurationVersion     = 2
	schemaHeader             = "# yaml-language-server: $schema=./schema/workflow-monitoring.schema.json\n"
)

var (
	workspaceIDPattern            = regexp.MustCompile(`^[1-9][0-9]*$`)
	unityCatalogIdentifierPattern = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_-]*$`)
	weekdayPattern                = regexp.MustCompile(
		`^(monday|tuesday|wednesday|thursday|friday|saturday|sunday)$`,
	)
)

type jobsAPIStorageConfiguration struct {
	Catalog string `yaml:"catalog"`
	Schema  string `yaml:"schema"`
}

type serviceLevelAgreement struct {
	Frequency         string `yaml:"frequency"`
	CompletionTime    string `yaml:"completion_time"`
	Timezone          string `yaml:"timezone,omitempty"`
	DayOfWeek         string `yaml:"day_of_week,omitempty"`
	FirstDeadlineDate string `yaml:"first_deadline_date,omitempty"`
	DayOfMonth        int    `yaml:"day_of_month,omitempty"`
}

type monitoredWorkflow struct {
	JobID            int64                 `yaml:"job_id"`
	MonitoringStatus string                `yaml:"monitoring_status"`
	SLA              serviceLevelAgreement `yaml:"sla"`
}

type monitoringConfiguration struct {
	Version          int                          `yaml:"version"`
	CollectorStorage *jobsAPIStorageConfiguration `yaml:"jobs_api_config,omitempty"`
	WorkspaceID      string                       `yaml:"workspace_id"`
	DefaultTimezone  string                       `yaml:"default_timezone"`
	Workflows        []monitoredWorkflow          `yaml:"workflows"`
}

type configurationDocument struct {
	configuration monitoringConfiguration
	documentNode  *yaml.Node
	rootNode      *yaml.Node
}

func initialConfiguration(
	workspaceID string,
	defaultTimezone string,
	catalogName string,
	schemaName string,
) (monitoringConfiguration, error) {
	if !workspaceIDPattern.MatchString(workspaceID) {
		return monitoringConfiguration{}, usageErrorf(
			"--workspace-id must contain digits and cannot start with zero",
		)
	}
	if timezoneError := validateTimezone(defaultTimezone); timezoneError != nil {
		return monitoringConfiguration{}, fmt.Errorf(
			"%w: --default-timezone: %v",
			errInvalidUsage,
			timezoneError,
		)
	}
	if (catalogName == "") != (schemaName == "") {
		return monitoringConfiguration{}, usageErrorf(
			"--catalog and --schema must be provided together",
		)
	}
	configuration := monitoringConfiguration{
		Version:         configurationVersion,
		WorkspaceID:     workspaceID,
		DefaultTimezone: defaultTimezone,
		Workflows:       []monitoredWorkflow{},
	}
	if catalogName == "" {
		return configuration, nil
	}
	if identifierError := validateUnityCatalogIdentifier(catalogName); identifierError != nil {
		return monitoringConfiguration{}, fmt.Errorf(
			"%w: --catalog: %v",
			errInvalidUsage,
			identifierError,
		)
	}
	if identifierError := validateUnityCatalogIdentifier(schemaName); identifierError != nil {
		return monitoringConfiguration{}, fmt.Errorf(
			"%w: --schema: %v",
			errInvalidUsage,
			identifierError,
		)
	}
	configuration.CollectorStorage = &jobsAPIStorageConfiguration{
		Catalog: catalogName,
		Schema:  schemaName,
	}
	return configuration, nil
}

func validateWorkflow(workflow monitoredWorkflow, defaultTimezone string) error {
	if workflow.JobID <= 0 {
		return usageErrorf("--job-id must be a positive integer")
	}
	if workflow.MonitoringStatus != "active" && workflow.MonitoringStatus != "inactive" {
		return usageErrorf("--status must be active or inactive")
	}
	if completionTimeError := validateCompletionTime(
		workflow.SLA.CompletionTime,
	); completionTimeError != nil {
		return fmt.Errorf(
			"%w: --completion-time: %v",
			errInvalidUsage,
			completionTimeError,
		)
	}
	timezoneName := workflow.SLA.Timezone
	if timezoneName == "" {
		timezoneName = defaultTimezone
	}
	if timezoneError := validateTimezone(timezoneName); timezoneError != nil {
		return fmt.Errorf("%w: --timezone: %v", errInvalidUsage, timezoneError)
	}
	return validateSchedule(workflow.SLA)
}

func validateSchedule(sla serviceLevelAgreement) error {
	switch sla.Frequency {
	case "daily":
		hasScheduleSpecificField := sla.DayOfWeek != "" ||
			sla.FirstDeadlineDate != "" ||
			sla.DayOfMonth != 0
		if hasScheduleSpecificField {
			return usageErrorf("daily SLA does not accept schedule-specific flags")
		}
	case "weekly":
		if !weekdayPattern.MatchString(sla.DayOfWeek) {
			return usageErrorf("weekly SLA requires --day-of-week")
		}
		if sla.FirstDeadlineDate != "" || sla.DayOfMonth != 0 {
			return usageErrorf("weekly SLA only accepts --day-of-week")
		}
	case "fortnightly":
		if _, dateError := time.Parse("2006-01-02", sla.FirstDeadlineDate); dateError != nil {
			return usageErrorf(
				"fortnightly SLA requires --first-deadline-date in YYYY-MM-DD",
			)
		}
		if sla.DayOfWeek != "" || sla.DayOfMonth != 0 {
			return usageErrorf(
				"fortnightly SLA only accepts --first-deadline-date",
			)
		}
	case "monthly":
		if sla.DayOfMonth < 1 || sla.DayOfMonth > 31 {
			return usageErrorf("monthly SLA requires --day-of-month from 1 to 31")
		}
		if sla.DayOfWeek != "" || sla.FirstDeadlineDate != "" {
			return usageErrorf("monthly SLA only accepts --day-of-month")
		}
	default:
		return usageErrorf(
			"--frequency must be daily, weekly, fortnightly, or monthly",
		)
	}
	return nil
}

func validateTimezone(candidate string) error {
	if _, loadError := time.LoadLocation(candidate); loadError != nil {
		return errors.New("use an IANA timezone such as UTC or Australia/Melbourne")
	}
	return nil
}

func validateCompletionTime(candidate string) error {
	if len(candidate) != len("06:00") {
		return errors.New("use 24-hour HH:MM")
	}
	if _, parseError := time.Parse("15:04", candidate); parseError != nil {
		return errors.New("use 24-hour HH:MM")
	}
	return nil
}

func validateUnityCatalogIdentifier(candidate string) error {
	if !unityCatalogIdentifierPattern.MatchString(candidate) {
		return errors.New(
			"use letters, digits, underscores, or hyphens; dots are not supported",
		)
	}
	return nil
}

func newConfigurationDocument(
	configuration monitoringConfiguration,
) (*configurationDocument, error) {
	documentNode := &yaml.Node{Kind: yaml.DocumentNode}
	rootNode := &yaml.Node{}
	if encodeError := rootNode.Encode(configuration); encodeError != nil {
		return nil, fmt.Errorf("encode configuration: %w", encodeError)
	}
	documentNode.Content = []*yaml.Node{rootNode}
	return &configurationDocument{
		configuration: configuration,
		documentNode:  documentNode,
		rootNode:      rootNode,
	}, nil
}

func loadConfigurationDocument(configurationPath string) (*configurationDocument, error) {
	configurationYAML, readError := os.ReadFile(configurationPath)
	if readError != nil {
		return nil, fmt.Errorf("read %s: %w", configurationPath, readError)
	}
	documentNode := &yaml.Node{}
	if parseError := yaml.Unmarshal(configurationYAML, documentNode); parseError != nil {
		return nil, fmt.Errorf("parse %s: %w", configurationPath, parseError)
	}
	if len(documentNode.Content) != 1 || documentNode.Content[0].Kind != yaml.MappingNode {
		return nil, errors.New("configuration root must be a YAML mapping")
	}
	rootNode := documentNode.Content[0]
	configuration := monitoringConfiguration{}
	if decodeError := rootNode.Decode(&configuration); decodeError != nil {
		return nil, fmt.Errorf("decode %s: %w", configurationPath, decodeError)
	}
	if validationError := validateConfiguration(configuration); validationError != nil {
		return nil, validationError
	}
	workflowsNode := mappingValue(rootNode, "workflows")
	if workflowsNode == nil || workflowsNode.Kind != yaml.SequenceNode {
		return nil, errors.New("workflows must be a YAML sequence")
	}
	return &configurationDocument{
		configuration: configuration,
		documentNode:  documentNode,
		rootNode:      rootNode,
	}, nil
}

func validateConfiguration(configuration monitoringConfiguration) error {
	if configuration.Version != configurationVersion {
		return fmt.Errorf("configuration version must be %d", configurationVersion)
	}
	if !workspaceIDPattern.MatchString(configuration.WorkspaceID) {
		return errors.New("workspace_id must contain digits and cannot start with zero")
	}
	if timezoneError := validateTimezone(configuration.DefaultTimezone); timezoneError != nil {
		return fmt.Errorf("default_timezone: %w", timezoneError)
	}
	if configuration.CollectorStorage != nil {
		if identifierError := validateUnityCatalogIdentifier(
			configuration.CollectorStorage.Catalog,
		); identifierError != nil {
			return fmt.Errorf("jobs_api_config.catalog: %w", identifierError)
		}
		if identifierError := validateUnityCatalogIdentifier(
			configuration.CollectorStorage.Schema,
		); identifierError != nil {
			return fmt.Errorf("jobs_api_config.schema: %w", identifierError)
		}
	}
	seenJobIDs := make(map[int64]struct{}, len(configuration.Workflows))
	for _, workflow := range configuration.Workflows {
		if validationError := validateWorkflow(
			workflow,
			configuration.DefaultTimezone,
		); validationError != nil {
			return fmt.Errorf("job %d: %w", workflow.JobID, validationError)
		}
		if _, isDuplicate := seenJobIDs[workflow.JobID]; isDuplicate {
			return fmt.Errorf("job id %d is configured more than once", workflow.JobID)
		}
		seenJobIDs[workflow.JobID] = struct{}{}
	}
	return nil
}

func (d *configurationDocument) workflowIndex(jobID int64) int {
	for workflowIndex, workflow := range d.configuration.Workflows {
		if workflow.JobID == jobID {
			return workflowIndex
		}
	}
	return -1
}

func (d *configurationDocument) appendWorkflow(workflow monitoredWorkflow) error {
	workflowsNode := mappingValue(d.rootNode, "workflows")
	workflowNode := &yaml.Node{}
	if encodeError := workflowNode.Encode(workflow); encodeError != nil {
		return fmt.Errorf("encode workflow: %w", encodeError)
	}
	workflowsNode.Content = append(workflowsNode.Content, workflowNode)
	d.configuration.Workflows = append(d.configuration.Workflows, workflow)
	return nil
}

func (d *configurationDocument) replaceWorkflow(
	workflowIndex int,
	workflow monitoredWorkflow,
) error {
	workflowsNode := mappingValue(d.rootNode, "workflows")
	workflowNode := &yaml.Node{}
	if encodeError := workflowNode.Encode(workflow); encodeError != nil {
		return fmt.Errorf("encode workflow: %w", encodeError)
	}
	existingWorkflowNode := workflowsNode.Content[workflowIndex]
	workflowNode.HeadComment = existingWorkflowNode.HeadComment
	workflowNode.LineComment = existingWorkflowNode.LineComment
	workflowNode.FootComment = existingWorkflowNode.FootComment
	workflowsNode.Content[workflowIndex] = workflowNode
	d.configuration.Workflows[workflowIndex] = workflow
	return nil
}

func (d *configurationDocument) removeWorkflow(workflowIndex int) {
	workflowsNode := mappingValue(d.rootNode, "workflows")
	workflowsNode.Content = append(
		workflowsNode.Content[:workflowIndex],
		workflowsNode.Content[workflowIndex+1:]...,
	)
	d.configuration.Workflows = append(
		d.configuration.Workflows[:workflowIndex],
		d.configuration.Workflows[workflowIndex+1:]...,
	)
}

func mappingValue(mappingNode *yaml.Node, key string) *yaml.Node {
	for contentIndex := 0; contentIndex+1 < len(mappingNode.Content); contentIndex += 2 {
		if mappingNode.Content[contentIndex].Value == key {
			return mappingNode.Content[contentIndex+1]
		}
	}
	return nil
}

func (d *configurationDocument) write(configurationPath string) error {
	var configurationBuffer bytes.Buffer
	encoder := yaml.NewEncoder(&configurationBuffer)
	encoder.SetIndent(2)
	if encodeError := encoder.Encode(d.documentNode); encodeError != nil {
		return fmt.Errorf("encode configuration: %w", encodeError)
	}
	if closeError := encoder.Close(); closeError != nil {
		return fmt.Errorf("close YAML encoder: %w", closeError)
	}
	configurationYAML := configurationBuffer.Bytes()
	if !bytes.HasPrefix(configurationYAML, []byte(schemaHeader)) {
		configurationYAML = append([]byte(schemaHeader), configurationYAML...)
	}
	if directoryError := os.MkdirAll(filepath.Dir(configurationPath), 0o755); directoryError != nil {
		return fmt.Errorf("create configuration directory: %w", directoryError)
	}
	configurationMode := os.FileMode(0o644)
	if fileDetails, statError := os.Stat(configurationPath); statError == nil {
		configurationMode = fileDetails.Mode().Perm()
	} else if !errors.Is(statError, os.ErrNotExist) {
		return fmt.Errorf("inspect configuration permissions: %w", statError)
	}
	if writeError := maybe.WriteFile(
		configurationPath,
		configurationYAML,
		configurationMode,
	); writeError != nil {
		return fmt.Errorf("write configuration: %w", writeError)
	}
	return nil
}
