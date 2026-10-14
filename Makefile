NAME_SERVER	= server
NAME_CLIENT	= client
NAME_GFX	= gfx

CXX			= c++
CXXFLAGS	= -Wall -Wextra -Werror -std=c++11 -MMD -MP
INCLUDES	= -I./includes
OBJ_DIR		= obj/

SRC_SERVER	= srcs/server/main.cpp \
			  srcs/core/Server.cpp \
			  srcs/core/Player.cpp
SRC_CLIENT	= srcs/client/client.py
SRC_GFX		= srcs/gfx/gfx.py

OBJ_SERVER	= $(patsubst %.cpp,$(OBJ_DIR)%.o,$(SRC_SERVER))
DEP_SERVER	= $(OBJ_SERVER:.o=.d)

all: $(NAME_SERVER) $(NAME_CLIENT) $(NAME_GFX)

$(OBJ_DIR)%.o: %.cpp
	@mkdir -p $(@D)
	$(CXX) $(CXXFLAGS) $(INCLUDES) -c $< -o $@

$(NAME_SERVER): $(OBJ_SERVER)
	$(CXX) $(CXXFLAGS) $(OBJ_SERVER) -o $(NAME_SERVER)

# The client and the graphic client are Python scripts: "building" them
# installs the script as an executable, only when the source changed.
$(NAME_CLIENT): $(SRC_CLIENT)
	cp $(SRC_CLIENT) $(NAME_CLIENT)
	chmod +x $(NAME_CLIENT)

$(NAME_GFX): $(SRC_GFX)
	cp $(SRC_GFX) $(NAME_GFX)
	chmod +x $(NAME_GFX)

clean:
	rm -rf $(OBJ_DIR)

fclean: clean
	rm -f $(NAME_SERVER) $(NAME_CLIENT) $(NAME_GFX)

re: fclean
	$(MAKE) all

-include $(DEP_SERVER)

.PHONY: all clean fclean re
