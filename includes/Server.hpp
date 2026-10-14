#pragma once
#include <poll.h>
#include <csignal>
#include <vector>
#include <map>
#include <set>
#include <string>
#include <chrono>
#include "Player.hpp"
#include "World.hpp"

extern volatile sig_atomic_t g_keep_running;

class Server {
	public:
		struct ServerOPTS {
			int port{};
			int x{};
			int y{};
			std::vector<std::string> teams{};
			int nbClients{};
			int time{};
		};
		ServerOPTS serverOPTS;

	private:
		struct Egg {
			int id;
			int parent_id;
			std::string team;
			int x;
			int y;
			Clock::time_point hatch_time;
			Clock::time_point death_time;	// only meaningful once hatched
			bool hatched = false;
		};

		struct Incantation {
			int x;
			int y;
			size_t level;
			Clock::time_point end_time;
			std::vector<int> fds;
		};

		// A client that does not read its data is dropped past this amount of pending output
		static const size_t MAX_OUTPUT_BUFFER = 64 * 1024 * 1024;
		// Bytes written per POLLOUT event: small enough to always fit in the free socket space
		static const size_t SEND_CHUNK = 4096;

		int m_serverSocket = -1;
		std::map<int, Player> m_clients;
		std::set<int> m_to_disconnect;
		std::map<std::string, int> m_team_slots;
		std::vector<Egg> m_eggs;
		int m_egg_id_counter = 0;
		int m_player_id_counter = 0;
		std::vector<Incantation> m_incantations;
		World m_world;
		Clock::time_point m_next_refill;
		bool m_game_over = false;
		Clock::time_point m_shutdown_deadline;

		// Time helpers
		Clock::duration units(double n) const;

		// Network
		void init_socket(void);
		void accept_client(void);
		void read_client(int fd);
		void flush_client(int fd);
		void send_to(int fd, const std::string &msg);
		void send_to_graphic(const std::string &msg);
		void disconnect_client(int fd);
		void process_input(int fd);
		void handshake(int fd, const std::string &line);
		void graphic_handshake(int fd);
		int compute_timeout(void);
		void set_time_unit(int t);
		bool all_output_flushed(void) const;

		// Game logic
		void update_game(void);
		void kill_player(int fd);
		void start_next_command(Player &p);
		void execute_player_command(int fd, const std::string &cmd);
		std::string build_vision_string(int self_fd, Player &player);
		int get_broadcast_dir(int src_x, int src_y, int dst_x, int dst_y, Player::Direction dir);
		size_t count_players_for_incantation(int x, int y, size_t level);
		bool check_incantation_stones(int x, int y, size_t level);
		void check_finished_incantations(void);
		void check_victory(void);
		int command_delay(const std::string &cmd) const;

		// Graphic protocol
		void handle_graphic_command(int fd, const std::string &line);
		Player *find_player_by_id(int id);
		std::string get_mct_string(void);
		std::string get_bct_string(int x, int y);
		std::string get_ppo_string(Player &p);
		std::string get_pnw_string(Player &p);
		std::string get_plv_string(Player &p);
		std::string get_pin_string(Player &p);

	public:
		Server() = default;
		Server(const Server &other) = delete;
		Server& operator=(const Server &other) = delete;
		~Server();
		void run(void);
};
