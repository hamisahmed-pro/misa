use std::io::Read;

#[derive(serde::Deserialize)]
struct Person {
    name: String,
    age: i64,
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() > 1 && args[1] == "hello" {
        println!("hello");
        return;
    }
    let mut buf = String::new();
    std::io::stdin().read_to_string(&mut buf).unwrap();
    let p: Person = serde_json::from_str(&buf).unwrap();
    println!("{} is {}", p.name, p.age);
}
