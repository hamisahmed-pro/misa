// jsoncli: read a JSON object from stdin, print name and age fields.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
)

type Person struct {
	Name string `json:"name"`
	Age  int    `json:"age"`
}

func main() {
	data, err := io.ReadAll(os.Stdin)
	if err != nil {
		fmt.Fprintln(os.Stderr, "read error:", err)
		os.Exit(1)
	}
	var p Person
	if err := json.Unmarshal(data, &p); err != nil {
		fmt.Fprintln(os.Stderr, "json error:", err)
		os.Exit(1)
	}
	fmt.Printf("%s is %d\n", p.Name, p.Age)
}
