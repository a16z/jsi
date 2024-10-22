use std::env;
use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process;
use std::time::Instant;

fn get_server_home() -> Option<PathBuf> {
    env::var_os("HOME").map(|home| {
        let mut path = PathBuf::from(home);
        path.push(".jsi");
        path.push("daemon");
        path
    })
}

fn send_command(command: &str) -> Result<(), Box<dyn std::error::Error>> {
    let socket_path = get_server_home().unwrap().join("server.sock");
    let mut stream = UnixStream::connect(socket_path)?;

    // Send the command
    stream.write_all(command.as_bytes())?;
    stream.flush()?;

    // Read the response
    let start = Instant::now();
    let mut response = String::new();
    stream.read_to_string(&mut response)?;

    println!("{}", response);
    println!("; response time: {:?}", start.elapsed());
    Ok(())
}

fn main() {
    let args: Vec<String> = env::args().collect();

    if args.len() < 2 {
        eprintln!("Usage: {} <command>", args[0]);
        process::exit(1);
    }

    let command = args[1..].join(" ");

    match send_command(&command) {
        Ok(_) => (),
        Err(e) => eprintln!("Error: {}", e),
    }
}
