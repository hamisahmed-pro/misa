package com.javacmp.app;

import android.app.Activity;
import android.os.Bundle;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ListView;
import java.util.ArrayList;

public class MainActivity extends Activity {
    private final ArrayList<String> items = new ArrayList<>();
    private ArrayAdapter<String> adapter;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        items.add("Buy milk");
        items.add("Write report");
        adapter = new ArrayAdapter<>(this,
            android.R.layout.simple_list_item_1, items);
        ((ListView) findViewById(R.id.taskList)).setAdapter(adapter);
        EditText input = findViewById(R.id.taskInput);
        Button add = findViewById(R.id.addButton);
        add.setOnClickListener(v -> {
            String t = input.getText().toString().trim();
            if (!t.isEmpty()) {
                items.add(t);
                adapter.notifyDataSetChanged();
                input.setText("");
            }
        });
    }
}
