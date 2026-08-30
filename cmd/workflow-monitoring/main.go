package main

import (
	"errors"
	"io"
	"os"
	_ "time/tzdata"
)

var buildVersion = "dev"

func main() {
	os.Exit(runCLI(os.Args[1:], os.Stdout, os.Stderr))
}

func runCLI(commandArguments []string, standardOutput, errorOutput io.Writer) int {
	rootCommand := newRootCommand()
	rootCommand.SetArgs(commandArguments)
	rootCommand.SetOut(standardOutput)
	rootCommand.SetErr(errorOutput)
	if commandError := rootCommand.Execute(); commandError != nil {
		_ = writeOutput(errorOutput, "%v\n", commandError)
		if errors.Is(commandError, errInvalidUsage) {
			return 2
		}
		return 1
	}
	return 0
}
